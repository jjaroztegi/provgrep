"""Distill the model (dense.MODEL) into a smaller one for queries: its token embeddings and LAYERS
of its layers, trained to point where it points in the space an index ranks code in (dense.py's
projection). A query is then a third of the CPU, and the suites rank as well.

`provgrep distill`, on a GPU, with torch, transformers and onnx installed (docs/build.md):

1. Query-like texts from the staged corpus (stage.py): the first sentence of a function's comment
   or docstring, its name split into words, a sentence with an identifier its code calls (people
   name APIs in questions), the first words of a sentence as typed. The suites' questions are left
   out: they are the test.
2. The model embeds them in half precision, a part at a time: a crashed run goes on where it was.
3. The student is trained on the texts of at most SHORT tokens (queries are short; long ones only
   cost GPU memory), exported to ONNX and quantized to 8 bits.
"""

import gzip
import hashlib
import json
import random
import re
import time
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

from provgrep import dense, stage
from provgrep.fetch import cache_dir, sha256_of
from provgrep.search import _parts

LAYERS = (0, 4, 8, 11)  # of the model's 12: 0 and 11 alone ranked worse on the suites
SHORT = 64  # tokens: the longest text trained on
BATCH = 256
RATE = 1e-4
VALID = 20_000  # texts held out to measure the student on
PART = 200_000  # texts embedded between two saves
DOCS, NAMES = 0.30, 0.03  # shares of functions whose doc, or name, makes a text
# The model's code on Hugging Face, which its weights' repository points to.
CODE = ("jinaai/jina-bert-v2-qk-post-norm", "3baf9e3ac750e76e8edd3019170176884695fb94")

_MARKERS = re.compile(
    r"^\s*(/\*\*|/\*!|/\*|\*/|\*|///|//!|//|#+|--+|;+|\(\*+|\{-|!+|%+|\"\"\"|''')\s?"
)
_TAIL = re.compile(r"\s*(\*/|\*\)|-\}|\"\"\"|''')\s*$")
_TAGS = re.compile(
    r"^\s*(@|:param|:return|:raises|Args:|Arguments:|Returns?:|Parameters|Raises:|Example|Note:"
    r"|TODO|FIXME|XXX|\\param|\\brief|\\return)",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"copyright|licen[cs]e|spdx|all rights reserved|warranty", re.IGNORECASE)
_END = re.compile(r"(?<=[a-z0-9\)])\.(\s|$)")
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
_WORD = re.compile(r"[A-Za-z]{2,}")


def sentence(doc: str) -> str | None:
    """The first sentence of a comment or docstring, without its markers and tags; None if it is
    not a few English words (code, a license, another language)."""
    lines: list[str] = []
    for raw in doc.splitlines():
        line = raw
        for _ in range(3):
            line = _MARKERS.sub("", line)
        line = _TAIL.sub("", line).strip()
        if not line or _TAGS.match(line):
            if lines:
                break
            continue
        lines.append(line)
        if len(lines) == 4:
            break
    text = " ".join(lines).replace("\\brief", "").strip()
    if end := _END.search(text):
        text = text[: end.start()]
    text = re.sub(r"\s+", " ", text).strip(" .:;,-")
    if not 3 <= len(text.split()) <= 30 or _LEGAL.search(text):
        return None
    if sum(c.isascii() for c in text) < 0.95 * len(text) or len(_WORD.findall(text)) < 3:
        return None
    if text.count("(") + text.count("{") + text.count("=") > 3:
        return None
    return text


def name(symbol: str) -> str | None:
    """A function's name as words (`AgentLauncher.writeAttachResult`: agent launcher write attach
    result), if it has two or more."""
    words = [part for word in re.findall(r"\w+", symbol) for part in _parts(word) if part.isalpha()]
    return " ".join(words[:10]) if len(words) >= 2 else None


def _texts_of(path: str) -> list[str]:
    """The texts of one staged repository, the same each run."""
    pick = random.Random(int(hashlib.sha256(Path(path).name.encode()).hexdigest()[:8], 16))
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            for unit in json.loads(line).get("units") or ():
                draw = pick.random()
                if draw < DOCS and unit.get("doc") and (text := sentence(unit["doc"])):
                    kind = pick.random()
                    if kind < 0.7:
                        out.append(text)
                    elif kind < 0.9:
                        calls = sorted(
                            {m for m in _IDENTIFIER.findall(unit["code"]) if len(_parts(m)) > 1}
                        )
                        named = pick.sample(calls, min(len(calls), pick.choice((1, 1, 2))))
                        out.append(" ".join([text, *named]))
                    elif len(words := text.split()) >= 5:
                        out.append(" ".join(words[: pick.randint(2, len(words) - 2)]))
                elif draw > 1 - NAMES and (text := name(unit.get("symbol", ""))):
                    out.append(text)
    return out


def texts(staging: Path, suites: Path) -> list[str]:
    """The distinct texts of every staged repository, without the suites' questions, shuffled."""
    held = {
        intent["query"].lower().strip()
        for path in suites.glob("*.json")
        for intent in json.loads(path.read_text(encoding="utf-8"))["intents"]
    }
    seen: set[str] = set()
    out = []
    with Pool() as pool:
        for found in pool.imap(_texts_of, sorted(map(str, staging.glob("*.jsonl.gz"))), 8):
            for text in found:
                if (key := text.lower()) not in seen and key not in held:
                    seen.add(key)
                    out.append(text)
    random.Random(0).shuffle(out)
    return out


def listed(out: Path, staging: Path, suites: Path) -> list[str]:
    """The texts out/texts.json keeps, listed from the staging the first time. A run that stopped
    goes on from them and the embeddings made of them, unless they were listed from another
    staging or other suites: then it stops rather than train on them."""
    held = hashlib.sha256(
        b"".join(path.read_bytes() for path in sorted(suites.glob("*.json")))
    ).hexdigest()
    made_from = {"staging": staging.name, "suites": held}
    path = out / "texts.json"
    if path.exists():
        kept = json.loads(path.read_text(encoding="utf-8"))
        if kept["made_from"] != made_from:
            raise ValueError(f"{out} holds the texts of other inputs: give another --out")
        return list(kept["texts"])
    if not any(staging.glob("*.jsonl.gz")):
        raise ValueError(f"nothing staged in {staging}: stage the corpus first")
    out.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps({"made_from": made_from, "texts": texts(staging, suites)}))
    part.replace(path)  # a stop while writing leaves no half list
    return list(json.loads(path.read_text(encoding="utf-8"))["texts"])


def distill(out: Path, suites: Path, epochs: int = 1) -> dict[str, Any]:
    """Make the student in out/model_quantized.onnx; return what it was made from and how close
    it came."""
    import torch
    from tokenizers import Tokenizer
    from transformers import AutoModel

    started = time.monotonic()
    corpus = listed(out, cache_dir() / "staging" / stage.key(), suites)
    tokenizer = Tokenizer.from_file(
        str(dense.file(dense.MODEL.repo, dense.MODEL.revision, *dense.MODEL.tokenizer))
    )
    tokenizer.enable_truncation(dense.MAX_TOKENS)
    ids = [e.ids for e in tokenizer.encode_batch(corpus)]
    lengths = np.array([len(i) for i in ids])
    gpu = torch.device("cuda")
    model = AutoModel.from_pretrained(
        dense.MODEL.repo,
        revision=dense.MODEL.revision,
        trust_remote_code=True,
        code_revision=CODE[1],
    )
    pad = tokenizer.token_to_id(dense.MODEL.pad)

    def batch(rows: Any) -> tuple[Any, Any]:
        width = int(lengths[rows].max())
        x = np.full((len(rows), width), pad, dtype=np.int64)
        for k, row in enumerate(rows):
            x[k, : lengths[row]] = ids[row]
        mask = (np.arange(width)[None, :] < lengths[rows][:, None]).astype(np.int64)
        return torch.tensor(x, device=gpu), torch.tensor(mask, device=gpu)

    def pooled(net: Any, x: Any, mask: Any) -> Any:
        states = net(input_ids=x, attention_mask=mask).last_hidden_state
        weights = mask[..., None].to(states.dtype)
        return (states * weights).sum(1) / weights.sum(1)

    teacher = model.half().to(gpu).eval()
    by_length = np.argsort(lengths, kind="stable")
    for n, start in enumerate(range(0, len(corpus), PART)):
        path = out / f"teacher-{n:03d}.npy"
        if path.exists():
            continue
        rows = by_length[start : start + PART]
        vectors = np.zeros((len(rows), dense.MODEL.dimensions), dtype=np.float16)
        at = 0
        with torch.no_grad():
            while at < len(rows):  # as many as fit in the token budget of dense's batches
                size = 1
                while (
                    at + size < len(rows)
                    and size < dense.MAX_BATCH
                    and (size + 1) * lengths[rows[at + size]] <= dense.BATCH_TOKENS
                ):
                    size += 1
                x, mask = batch(rows[at : at + size])
                v = torch.nn.functional.normalize(pooled(teacher, x, mask).float(), dim=1)
                vectors[at : at + len(x)] = v.cpu().numpy()
                at += len(x)
        np.save(out / "part.npy", vectors)
        (out / "part.npy").replace(path)
    target = np.zeros((len(corpus), dense.MODEL.dimensions), dtype=np.float16)
    for n, start in enumerate(range(0, len(corpus), PART)):
        target[by_length[start : start + PART]] = np.load(out / f"teacher-{n:03d}.npy")

    student = model.float()
    student.encoder.layer = torch.nn.ModuleList(student.encoder.layer[i] for i in LAYERS)
    student.config.num_hidden_layers = len(LAYERS)
    student = student.to(gpu).train()
    components = torch.tensor(dense.store().projection().components, device=gpu)
    pick = np.random.default_rng(0)
    order = np.arange(len(corpus))
    valid = order[-VALID:][lengths[order[-VALID:]] <= SHORT]
    train = order[:-VALID][lengths[order[:-VALID]] <= SHORT]

    def loss(rows: Any) -> Any:
        x, mask = batch(rows)
        y = torch.tensor(target[rows].astype(np.float32), device=gpu)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            v = pooled(student, x, mask).float()
        return (1 - torch.nn.functional.cosine_similarity(v @ components, y @ components)).mean()

    steps = epochs * (len(train) // BATCH)
    optimizer = torch.optim.AdamW(student.parameters(), lr=RATE, weight_decay=0.01)
    schedule = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, RATE, total_steps=steps, pct_start=0.03
    )
    for _ in range(epochs):
        shuffled = pick.permutation(train)
        batches = []
        for chunk in np.array_split(shuffled, max(1, len(shuffled) // (BATCH * 64))):
            chunk = chunk[np.argsort(lengths[chunk], kind="stable")]  # like lengths: less padding
            batches += [chunk[i : i + BATCH] for i in range(0, len(chunk) - BATCH + 1, BATCH)]
        for done, rows in enumerate(pick.permutation(len(batches)), 1):
            optimizer.zero_grad()
            loss(batches[rows]).backward()
            torch.nn.utils.clip_grad_norm_(student.parameters(), 1.0)
            optimizer.step()
            schedule.step()
            if done % 2000 == 0:
                print(f"{done}/{steps} {time.monotonic() - started:.0f} s", flush=True)
    student.eval()
    with torch.no_grad():
        parts = [valid[i : i + 1024] for i in range(0, len(valid), 1024)]
        closeness = 1 - sum(float(loss(part)) * len(part) for part in parts) / len(valid)
    _export(student.float().cpu(), out)
    return {
        "model": {"repo": dense.MODEL.repo, "revision": dense.MODEL.revision},
        "layers": list(LAYERS),
        "texts": len(corpus),
        "trained_on": len(train),
        "valid_cosine": round(closeness, 4),
        "sha256": sha256_of(out / "model_quantized.onnx"),
        "seconds": round(time.monotonic() - started),
    }


def _export(student: Any, out: Path) -> None:
    """The student as ONNX (the same inputs and output as the model's file), then in 8 bits."""
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic

    class States(torch.nn.Module):  # type: ignore[misc]  # torch has no stubs where it is absent
        def __init__(self, net: Any) -> None:
            super().__init__()
            self.net = net

        def forward(self, input_ids: Any, attention_mask: Any) -> Any:
            return self.net(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state

    example = torch.tensor([[0, 100, 200, 2]])
    axes = {0: "batch", 1: "tokens"}
    torch.onnx.export(
        States(student),
        (example, torch.ones_like(example)),
        str(out / "model.onnx"),
        input_names=["input_ids", "attention_mask"],
        output_names=["last_hidden_state"],
        dynamic_axes={"input_ids": axes, "attention_mask": axes, "last_hidden_state": axes},
        opset_version=17,
        dynamo=False,
    )
    quantize_dynamic(
        str(out / "model.onnx"), str(out / "model_quantized.onnx"), weight_type=QuantType.QInt8
    )
