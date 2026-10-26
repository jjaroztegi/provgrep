// The page works without this: it only fetches the same results as you type, keeps the URL in
// step so a search can still be shared, and copies a snippet with its attribution.

const form = document.querySelector("form.search");
let timer;
let pending;

function params() {
  const query = new URLSearchParams(new FormData(form));
  for (const [key, value] of [...query]) {
    if (!value || (key === "license" && value === "permissive")) query.delete(key);
  }
  if (query.get("license") !== "allowed") query.delete("allowed");
  if (query.get("license") !== "compatible") query.delete("project");
  return query;
}

async function update() {
  const query = params();
  const words = query.get("q");
  history.replaceState(null, "", query.size ? `/?${query}` : "/");
  document.title = words ? `${words} - provgrep` : "provgrep";
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
  form.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(update, 250);
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
      copy.textContent = "Copied with attribution";
      setTimeout(() => { copy.textContent = "Copy with attribution"; }, 2000);
    } catch {
      // No clipboard here (an insecure origin, a refused permission): show it selected instead.
      attribution.hidden = false;
      getSelection().selectAllChildren(attribution);
      copy.textContent = "Selected below, ready to copy";
    }
  }
  const more = event.target.closest("button.more");
  if (more) more.closest(".code").classList.add("open");
});
