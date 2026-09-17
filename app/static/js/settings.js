/* Shared logic for the two settings pages: 🔑 API (keys and addresses in .env) and
   ⚙️ Innstillinger (choices in the database).

   Nothing here knows which fields a page has. Every input carries
   data-setting="<name>" plus data-kind="secret|text|number|bool", so the same
   collect/save/test helpers serve both pages and neither needs its own copy.
   A page that has none of these elements is left alone. */

(function () {
  "use strict";

  function escapeText(value) {
    const box = document.createElement("div");
    box.textContent = value == null ? "" : String(value);
    return box.innerHTML;
  }

  function fieldValue(el) {
    // Disabled means the value comes from an environment variable: never send it.
    if (el.disabled) return undefined;
    const kind = el.dataset.kind || "text";
    if (kind === "bool") return el.checked;
    if (kind === "number") return Number(el.value || 0);
    // A secret is only sent when the user actually typed a new one.
    if (kind === "secret") return el.dataset.dirty ? el.value : undefined;
    const value = el.value.trim();
    if (value !== "") return value;
    // An empty text field is left alone, unless it must be able to clear a choice.
    return el.dataset.allowEmpty !== undefined ? "" : undefined;
  }

  function collectSettings() {
    const payload = {};
    document.querySelectorAll("[data-setting]").forEach(function (el) {
      const value = fieldValue(el);
      if (value !== undefined) payload[el.dataset.setting] = value;
    });
    if (typeof window.collectExtra === "function") {
      Object.assign(payload, window.collectExtra());
    }
    return payload;
  }

  function postSettings(payload) {
    return api("/api/settings", { method: "POST", body: payload });
  }

  async function saveSettings(button) {
    const payload = collectSettings();
    const save = async function () {
      const result = await postSettings(payload);
      if (!result.ok) {
        toast(errorMessage(result, "Kunne ikke lagre."), "error");
        return false;
      }
      toast(result.data.message, "ok");
      setTimeout(function () { location.reload(); }, 1200);
      return true;
    };
    return button ? withBusy(button, save) : save();
  }

  async function clearSecret(fieldId, label, button) {
    const yes = await confirmDialog(
      "Fjerne " + label + "?",
      "Verdien slettes fra .env. Du kan legge den inn igjen når som helst.",
      "Fjern"
    );
    if (!yes) return;
    await withBusy(button, async function () {
      const payload = {};
      payload[fieldId] = "";
      const result = await postSettings(payload);
      if (!result.ok) return toast(errorMessage(result, "Kunne ikke fjerne verdien."), "error");
      toast(label + " er fjernet.", "ok");
      setTimeout(function () { location.reload(); }, 1000);
    });
  }

  async function testProvider(provider, button) {
    const box = document.getElementById("test-" + provider + "-result");
    // What is typed right now is tested even if it is not saved; empty fields
    // fall back to the saved value on the server.
    const body = {};
    document.querySelectorAll('[data-test-provider="' + provider + '"]').forEach(function (el) {
      if (!el.disabled && el.value.trim()) body[el.dataset.setting] = el.value.trim();
    });
    await withBusy(button, async function () {
      const result = await api("/api/settings/test/" + provider, { method: "POST", body: body });
      const data = result.data || {};
      const ok = result.ok && data.ok;
      const detail = data.detail || errorMessage(result, "Ukjent feil");
      if (box) {
        box.innerHTML =
          '<span class="badge ' + (ok ? "badge-ok" : "badge-error") + '">' +
          (ok ? "✓ " : "✕ ") + escapeText(detail) + "</span>";
      }
      toast(detail, ok ? "ok" : "error");
    });
  }

  function fillModels() {
    const provider = document.getElementById("ai_provider");
    const select = document.getElementById("ai_model");
    if (!provider || !select) return;
    const models = (window.AI_MODELS || {})[provider.value] || [];
    select.innerHTML = "";
    if (!models.length) {
      select.innerHTML = '<option value="">— ikke relevant —</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    models.forEach(function (m) {
      const option = document.createElement("option");
      option.value = m.id;
      option.textContent = m.label + " (" + m.price + ")";
      if (m.id === window.CURRENT_AI_MODEL) option.selected = true;
      select.appendChild(option);
    });
  }

  async function loadVoices(button) {
    await withBusy(button, async function () {
      const [voices, models] = await Promise.all([
        api("/api/elevenlabs/voices"),
        api("/api/elevenlabs/models"),
      ]);
      if (!voices.ok) return toast(errorMessage(voices, "Kunne ikke hente stemmer."), "error");
      const voiceSelect = document.getElementById("elevenlabs_voice_id");
      const current = voiceSelect.value;
      voiceSelect.innerHTML = '<option value="">— ikke valgt —</option>';
      voices.data.forEach(function (v) {
        const option = document.createElement("option");
        option.value = v.voice_id;
        option.textContent = v.name + (v.category ? " · " + v.category : "");
        if (v.voice_id === current) option.selected = true;
        voiceSelect.appendChild(option);
      });

      if (models.ok) {
        const modelSelect = document.getElementById("elevenlabs_model_id");
        const currentModel = modelSelect.value;
        modelSelect.innerHTML = '<option value="">— ikke valgt —</option>';
        models.data.forEach(function (m) {
          const option = document.createElement("option");
          option.value = m.model_id;
          option.textContent = m.name;
          if (m.model_id === currentModel) option.selected = true;
          modelSelect.appendChild(option);
        });
      }
      toast("Hentet " + voices.data.length + " stemmer.", "ok");
    });
  }

  window.collectSettings = collectSettings;
  window.saveSettings = saveSettings;
  window.clearSecret = clearSecret;
  window.testProvider = testProvider;
  window.fillModels = fillModels;
  window.loadVoices = loadVoices;

  document.addEventListener("DOMContentLoaded", function () {
    fillModels();
    document.querySelectorAll('input[type="range"][data-setting]').forEach(function (input) {
      const out = document.getElementById(input.id + "_out");
      if (out) input.addEventListener("input", function () { out.textContent = input.value; });
    });
    document.querySelectorAll('input[type="password"][data-setting]').forEach(function (el) {
      el.addEventListener("input", function () { el.dataset.dirty = "1"; });
    });
  });
})();
