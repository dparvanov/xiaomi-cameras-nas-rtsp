document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-stream]");
  if (!button) return;

  const input = button.closest(".stream-copy-row")?.querySelector(".stream-url");
  if (!input) return;

  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(input.value);
      copied = true;
    }
  } catch (_) {
    copied = false;
  }

  if (!copied) {
    input.focus();
    input.select();
    copied = document.execCommand("copy");
  }

  const originalText = button.textContent;
  button.textContent = copied ? "Copied" : "Select and copy";
  window.setTimeout(() => {
    button.textContent = originalText;
  }, 1600);
});
