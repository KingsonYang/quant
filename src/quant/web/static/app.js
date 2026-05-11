document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[method='post']").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-submit-button]");
      const status = form.querySelector("[data-run-status]");
      if (button) {
        button.disabled = true;
      }
      if (status) {
        status.hidden = false;
      }
    });
  });
});
