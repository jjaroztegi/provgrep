// The page works without this: it only fetches the same results as you type or pick a filter,
// keeps the URL in step so a search can still be shared, and copies a snippet with its
// attribution.

const form = document.getElementById("search");
const filters = document.querySelector(".filters");
let timer;
let pending;

function params() {
  const query = new URLSearchParams(new FormData(form));  // the filters too: they name the form
  for (const [key, value] of [...query]) {
    if (!value || (key === "license" && value === "permissive")) query.delete(key);
  }
  if (query.get("license") !== "allowed") query.delete("allowed");
  if (query.get("license") !== "compatible") query.delete("project");
  return query;
}

// What the folded filters say they are set to, as the server first writes it (filters.html).
function describe() {
  const f = form.elements;
  const license = f.license.value;
  const named = { allowed: f.allowed.value || "only these", compatible: `fits ${f.project.value}` };
  const level = f.level.value;
  return `${named[license] ?? `${license} license`}, ${f.language.value || "every language"}, ${
    level ? `level ${level}` : "any level"
  }`;
}

async function update() {
  const query = params();
  const words = query.get("q");
  history.replaceState(null, "", query.size ? `/?${query}` : "/");
  document.title = words ? `${words} | provgrep` : "provgrep | copy-paste, with receipts";  // as base.html
  if (filters) filters.querySelector(".state").textContent = describe();
  query.set("partial", "1");  // without words, the page from before a search
  pending?.abort();
  pending = new AbortController();
  try {
    const response = await fetch(`/?${query}`, { signal: pending.signal });
    const main = document.getElementById("main");
    main.innerHTML = await response.text();
  } catch (error) {
    if (error.name !== "AbortError") throw error;
  }
}

if (form) {
  document.body.classList.add("live");
  // On a phone the filters start folded; without the script they stay open.
  if (filters && matchMedia("(max-width: 760px)").matches) filters.open = false;
  document.addEventListener("input", (event) => {
    if (event.target.form !== form) return;
    clearTimeout(timer);
    const typed = event.target.type === "search" || event.target.type === "text";
    timer = setTimeout(update, typed ? 250 : 0);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(timer);
    update();
  });
}

document.addEventListener("click", async (event) => {
  const copy = event.target.closest("button.copy");
  if (copy) {
    const attribution = document.getElementById(copy.dataset.copy);
    try {
      await navigator.clipboard.writeText(attribution.textContent);
      copy.textContent = "copied with attribution";
      setTimeout(() => { copy.textContent = "copy with attribution"; }, 2000);
    } catch {
      // No clipboard here (an insecure origin, a refused permission): show it selected instead.
      attribution.hidden = false;
      getSelection().selectAllChildren(attribution);
      copy.textContent = "selected below, ready to copy";
    }
  }
  const more = event.target.closest("button.more");
  if (more) more.closest(".code").classList.add("open");
});
