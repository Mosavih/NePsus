// Progressive enhancement only — every page works without JS.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("details").forEach((d) => {
    d.addEventListener("toggle", () => {
      try {
        sessionStorage.setItem("nx-open-" + d.querySelector("summary").textContent,
          d.open ? "1" : "0");
      } catch (e) { /* private mode */ }
    });
  });
});
