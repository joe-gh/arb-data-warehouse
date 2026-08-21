// Disable the sign-in button on submit so a double click cannot send the
// credentials twice. Lives in a file because the CSP forbids inline scripts.
document.addEventListener("DOMContentLoaded", function () {
  var form = document.querySelector("form.auth-form") || document.querySelector("main form");
  if (!form) return;
  form.addEventListener("submit", function () {
    var button = form.querySelector("button[type='submit']");
    if (button) {
      button.disabled = true;
      button.textContent = "Signing in...";
    }
  });
});
