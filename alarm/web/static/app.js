(() => {
  "use strict";

  const toast = document.getElementById("app-toast");
  const announcer = document.getElementById("assertive-announcer");
  const overlay = document.getElementById("ringing-overlay");
  let overlayWasOpen = false;
  let toastTimer;

  function notify(message, urgent = false) {
    if (toast) {
      toast.textContent = message;
      toast.classList.remove("hidden");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.add("hidden"), 4200);
    }
    if (urgent && announcer) announcer.textContent = message;
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.issues?.map((issue) => issue.message).join("; ") || body.error || `Request failed (${response.status})`;
      throw new Error(detail);
    }
    return body;
  }

  async function runAction(element) {
    const url = element.dataset.post || element.dataset.delete;
    const confirmation = element.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) return;
    element.disabled = true;
    try {
      await requestJson(url, {
        method: element.dataset.delete ? "DELETE" : "POST",
        body: element.dataset.body || "{}",
      });
      notify(element.dataset.success || "Action completed");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      notify(error.message, true);
      element.disabled = false;
    }
  }

  document.addEventListener("click", (event) => {
    const action = event.target.closest("[data-post], [data-delete]");
    if (action) runAction(action);
  });

  const search = document.getElementById("alarm-search");
  if (search) {
    search.addEventListener("input", () => {
      const query = search.value.trim().toLowerCase();
      const cards = [...document.querySelectorAll("[data-alarm-card]")];
      let visible = 0;
      cards.forEach((card) => {
        const match = card.dataset.search.includes(query);
        card.classList.toggle("hidden", !match);
        if (match) visible += 1;
      });
      document.getElementById("alarm-search-empty")?.classList.toggle("hidden", visible !== 0);
    });
  }

  function updateRinging(ringing) {
    if (!overlay) return;
    overlay.classList.toggle("hidden", !ringing);
    overlay.classList.toggle("flex", Boolean(ringing));
    document.body.classList.toggle("overflow-hidden", Boolean(ringing));
    if (!ringing) {
      overlayWasOpen = false;
      return;
    }
    document.getElementById("ringing-time").textContent = ringing.time || "Alarm";
    document.getElementById("ringing-title").textContent = ringing.label || "Alarm";
    document.querySelectorAll('[data-ring-action="snooze"]').forEach((button) => {
      button.classList.toggle("hidden", !ringing.snoozable);
    });
    if (!overlayWasOpen) {
      overlayWasOpen = true;
      announcer.textContent = `${ringing.label || "Alarm"} is ringing`;
      overlay.querySelector('[data-ring-action="dismiss"]')?.focus();
    }
  }

  async function pollStatus() {
    try {
      const status = await requestJson("/api/status");
      document.querySelectorAll(".js-clock").forEach((clock) => { clock.textContent = status.now; });
      updateRinging(status.ringing);
    } catch (error) {
      document.querySelectorAll(".js-clock").forEach((clock) => { clock.textContent = "offline"; });
    }
  }

  document.querySelectorAll("[data-ring-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const snooze = button.dataset.ringAction === "snooze";
        const body = snooze ? JSON.stringify({ minutes: Number(button.dataset.minutes || 5) }) : "{}";
        const result = await requestJson(snooze ? "/api/ringing/snooze" : "/api/ringing/dismiss", { method: "POST", body });
        if ((snooze && !result.snoozed) || (!snooze && !result.dismissed)) throw new Error("The alarm state changed before that action completed");
        notify(snooze ? `Alarm snoozed for ${button.dataset.minutes || 5} minutes` : "Alarm dismissed", true);
        await pollStatus();
      } catch (error) {
        notify(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });

  overlay?.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const focusable = [...overlay.querySelectorAll("button:not(.hidden):not([disabled])")];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  pollStatus();
  window.setInterval(pollStatus, 2000);
})();
