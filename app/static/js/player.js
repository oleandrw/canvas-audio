/* Minimal audio player: play/pause, scrubbable timeline, elapsed and total time.
   Progressive enhancement over a plain <audio> element, which stays available
   as a fallback if JavaScript fails. */

(function () {
  "use strict";

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m + ":" + String(s).padStart(2, "0");
  }

  function setup(root) {
    if (root.dataset.playerReady === "1") return;
    root.dataset.playerReady = "1";

    const audio = root.querySelector("audio");
    const button = root.querySelector(".player-btn");
    const timeline = root.querySelector(".player-timeline");
    const progress = root.querySelector(".player-progress");
    const current = root.querySelector('[data-role="current"]');
    const duration = root.querySelector('[data-role="duration"]');
    if (!audio || !button || !timeline) return;

    audio.controls = false;

    function render() {
      const total = audio.duration;
      const ratio = total ? audio.currentTime / total : 0;
      progress.style.width = (ratio * 100).toFixed(2) + "%";
      current.textContent = formatTime(audio.currentTime);
      if (isFinite(total)) duration.textContent = formatTime(total);
    }

    button.addEventListener("click", function () {
      if (audio.paused) {
        // Only one player at a time.
        document.querySelectorAll(".player audio").forEach(function (other) {
          if (other !== audio) other.pause();
        });
        audio.play().catch(function () {
          if (window.toast) window.toast("Kunne ikke spille av lydfilen.", "error");
        });
      } else {
        audio.pause();
      }
    });

    function seek(event) {
      const rect = timeline.getBoundingClientRect();
      const x = (event.touches ? event.touches[0].clientX : event.clientX) - rect.left;
      const ratio = Math.min(Math.max(x / rect.width, 0), 1);
      if (isFinite(audio.duration)) audio.currentTime = ratio * audio.duration;
      render();
    }

    timeline.addEventListener("click", seek);
    timeline.addEventListener("mousedown", function () {
      function move(e) { seek(e); }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });

    audio.addEventListener("play", function () { button.textContent = "⏸"; });
    audio.addEventListener("pause", function () { button.textContent = "▶"; });
    audio.addEventListener("ended", function () { button.textContent = "▶"; render(); });
    audio.addEventListener("timeupdate", render);
    audio.addEventListener("loadedmetadata", render);
    audio.addEventListener("error", function () {
      if (window.toast) window.toast("Lydfilen kunne ikke lastes.", "error");
    });

    render();
  }

  function setupAll() {
    document.querySelectorAll(".player").forEach(setup);
  }

  document.addEventListener("DOMContentLoaded", setupAll);
  document.body && document.addEventListener("htmx:afterSwap", setupAll);
})();
