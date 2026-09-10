/* Small helpers shared by every page. No framework, no build step. */

(function () {
  "use strict";

  // ---------------------------------------------------------------- toasts

  function toastStack() {
    let stack = document.querySelector(".toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      document.body.appendChild(stack);
    }
    return stack;
  }

  function toast(message, kind) {
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.innerHTML =
      '<span class="alert-icon">' +
      (kind === "error" ? "⚠️" : kind === "ok" ? "✅" : "ℹ️") +
      "</span><div></div>";
    el.lastChild.textContent = message;
    toastStack().appendChild(el);
    setTimeout(function () {
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 250);
    }, kind === "error" ? 8000 : 4500);
  }

  window.toast = toast;

  // ------------------------------------------------------------------ http

  async function api(url, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.body && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["Content-Type"] = "application/json";
    }
    const response = await fetch(url, opts);
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch (e) { payload = { detail: text }; }
    }
    return { ok: response.ok, status: response.status, data: payload };
  }

  function errorMessage(result, fallback) {
    if (!result || !result.data) return fallback;
    return result.data.detail || result.data.message || fallback;
  }

  window.api = api;
  window.errorMessage = errorMessage;

  // -------------------------------------------------------------- busy UI

  function withBusy(button, fn) {
    if (!button) return fn();
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>' + button.textContent.trim();
    return Promise.resolve()
      .then(fn)
      .finally(function () {
        button.disabled = false;
        button.innerHTML = original;
      });
  }

  window.withBusy = withBusy;

  // ---------------------------------------------------------------- format

  function money(value, symbol) {
    const amount = Number(value || 0);
    const digits = amount > 0 && amount < 0.01 ? 4 : 2;
    return (symbol || "$") + amount.toFixed(digits);
  }

  function number(value) {
    return new Intl.NumberFormat("nb-NO").format(Number(value || 0));
  }

  window.fmt = { money: money, number: number };

  // ---------------------------------------------------------- confirm modal

  function confirmDialog(title, message, confirmLabel) {
    return new Promise(function (resolve) {
      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true">' +
        "<h2></h2><p class=\"muted\"></p>" +
        '<div class="modal-actions">' +
        '<button class="btn btn-ghost" data-act="cancel">Avbryt</button>' +
        '<button class="btn btn-primary" data-act="ok"></button>' +
        "</div></div>";
      backdrop.querySelector("h2").textContent = title;
      backdrop.querySelector("p").textContent = message;
      backdrop.querySelector('[data-act="ok"]').textContent = confirmLabel || "Fortsett";

      function close(value) {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
        resolve(value);
      }
      function onKey(event) {
        if (event.key === "Escape") close(false);
      }

      backdrop.addEventListener("click", function (event) {
        const act = event.target.getAttribute && event.target.getAttribute("data-act");
        if (act === "ok") close(true);
        else if (act === "cancel" || event.target === backdrop) close(false);
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(backdrop);
      backdrop.querySelector('[data-act="ok"]').focus();
    });
  }

  window.confirmDialog = confirmDialog;

  // ------------------------------------------------------- collapsible tree

  document.addEventListener("click", function (event) {
    const head = event.target.closest(".module-head");
    if (!head) return;
    if (event.target.matches("input, label, a, button")) return;
    head.parentElement.classList.toggle("collapsed");
  });
})();
