// Login/register gate (goal.md ADR-018). Cookie-based sessions ride along on
// every same-origin fetch automatically — talk.js needs no changes at all;
// it just never becomes reachable until #app is unhidden below.

const authGate = document.querySelector("#auth-gate");
const app = document.querySelector("#app");
const authForm = document.querySelector("#auth-form");
const authHeading = document.querySelector("#auth-heading");
const authError = document.querySelector("#auth-error");
const authEmail = document.querySelector("#auth-email");
const authPassword = document.querySelector("#auth-password");
const authSubmit = document.querySelector("#auth-submit");
const authToggle = document.querySelector("#auth-toggle");
const authBadge = document.querySelector("#auth-badge");
const authLogout = document.querySelector("#auth-logout");

let mode = "login"; // or "register"

function showError(message) {
  authError.textContent = message;
  authError.hidden = !message;
}

function setMode(next) {
  mode = next;
  const isLogin = mode === "login";
  authHeading.textContent = isLogin ? "Log in" : "Create an account";
  authSubmit.textContent = isLogin ? "Log in" : "Register";
  authToggle.textContent = isLogin ? "Need an account? Register" : "Have an account? Log in";
  authPassword.autocomplete = isLogin ? "current-password" : "new-password";
  showError("");
}

function showApp(email) {
  authGate.hidden = true;
  app.hidden = false;
  authBadge.textContent = `Logged in as ${email}`;
}

function showGate() {
  app.hidden = true;
  authGate.hidden = false;
  authEmail.value = "";
  authPassword.value = "";
}

async function checkSession() {
  try {
    const response = await fetch("/auth/me");
    if (!response.ok) return showGate();
    const payload = await response.json();
    showApp(payload.email);
  } catch {
    showGate();
  }
}

authToggle.addEventListener("click", () => setMode(mode === "login" ? "register" : "login"));

authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  authSubmit.disabled = true;
  try {
    const response = await fetch(`/auth/${mode === "login" ? "login" : "register"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: authEmail.value, password: authPassword.value }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Request failed");
    showApp(payload.email);
  } catch (error) {
    showError(error.message);
  } finally {
    authSubmit.disabled = false;
  }
});

authLogout.addEventListener("click", async () => {
  try {
    await fetch("/auth/logout", { method: "POST" });
  } finally {
    showGate();
  }
});

setMode("login");
checkSession();
