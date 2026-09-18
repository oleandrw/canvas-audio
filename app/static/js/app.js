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
      '<span class="alert-icon" aria-hidden="true">' +
      (kind === "error" ? "⚠️" : kind === "ok" ? "✅" : "ℹ️") +
      '</span><div></div>' +
      '<button type="button" class="toast-close" aria-label="Lukk melding">✕</button>';
    el.children[1].textContent = message;
    // An error is read out at once and interrupts; the rest wait their turn.
    if (kind === "error") el.setAttribute("role", "alert");

    function dismiss() {
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 250);
    }
    el.querySelector(".toast-close").addEventListener("click", dismiss);
    toastStack().appendChild(el);

    // An error stays until it is dismissed: it is the one message that must not
    // disappear before it has been read.
    if (kind === "error") return;
    let timer = setTimeout(dismiss, 4500);
    el.addEventListener("mouseenter", function () { clearTimeout(timer); });
    el.addEventListener("focusin", function () { clearTimeout(timer); });
    el.addEventListener("mouseleave", function () { timer = setTimeout(dismiss, 2500); });
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
      const heading = backdrop.querySelector("h2");
      const text = backdrop.querySelector("p");
      const id = "modal-" + Date.now();
      heading.id = id + "-title";
      text.id = id + "-text";
      backdrop.querySelector(".modal").setAttribute("aria-labelledby", heading.id);
      backdrop.querySelector(".modal").setAttribute("aria-describedby", text.id);
      heading.textContent = title;
      text.textContent = message;
      backdrop.querySelector('[data-act="ok"]').textContent = confirmLabel || "Fortsett";

      const opener = document.activeElement;
      const outside = [document.querySelector("header.topbar"), document.getElementById("main")];
      outside.forEach(function (el) { if (el) el.inert = true; });

      function close(value) {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
        outside.forEach(function (el) { if (el) el.inert = false; });
        restoreFocus(opener);
        resolve(value);
      }
      function restoreFocus(element) {
        if (element && typeof element.focus === "function") element.focus();
      }
      function trapFocus(event) {
        const stops = backdrop.querySelectorAll("button:not([disabled])");
        if (!stops.length) return;
        const first = stops[0];
        const last = stops[stops.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
      function onKey(event) {
        if (event.key === "Escape") close(false);
        else if (event.key === "Tab") trapFocus(event);
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

})();
