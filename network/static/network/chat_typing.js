/* static/network/chat_typing.js - HTTP polling typing fallback */

(function () {
  const input = document.getElementById("chat-message-input");
  const indicator = document.getElementById("typing-indicator");
  const text = document.getElementById("typing-text");
  const form = document.getElementById("chat-message-form");

  if (!input || !indicator || !text) return;

  let typingTimer = null;

  function csrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function postTyping(isTyping) {
    if (!window.TYPING_SET_URL) return;

    const data = new URLSearchParams();
    data.append("is_typing", isTyping ? "true" : "false");

    fetch(window.TYPING_SET_URL, {
      method: "POST",
      headers: {
        "X-CSRFToken": csrfToken(),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: data.toString(),
    }).catch(() => {});
  }

  function renderTyping(users) {
    if (!users.length) {
      indicator.classList.add("hidden");
      indicator.classList.remove("flex");
      return;
    }

    if (users.length === 1) {
      text.textContent = `${users[0]} is typing...`;
    } else if (users.length === 2) {
      text.textContent = `${users[0]} and ${users[1]} are typing...`;
    } else {
      text.textContent = `${users.length} people are typing...`;
    }

    indicator.classList.remove("hidden");
    indicator.classList.add("flex");
  }

  async function refreshTypingUsers() {
    if (!window.TYPING_USERS_URL) return;

    try {
      const response = await fetch(window.TYPING_USERS_URL, {
        cache: "no-store",
      });

      if (!response.ok) return;

      const data = await response.json();
      renderTyping(data.users || []);
    } catch (error) {
      // Typing fallback is optional; silently wait for the next poll.
    }
  }

  input.addEventListener("input", function () {
    const hasText = input.value.trim().length > 0;

    if (!hasText) {
      window.clearTimeout(typingTimer);
      postTyping(false);
      return;
    }

    postTyping(true);
    window.clearTimeout(typingTimer);
    typingTimer = window.setTimeout(() => postTyping(false), 1400);
  });

  input.addEventListener("blur", function () {
    window.clearTimeout(typingTimer);
    postTyping(false);
  });

  if (form) {
    form.addEventListener("submit", function () {
      window.clearTimeout(typingTimer);
      postTyping(false);
    });
  }

  window.addEventListener("beforeunload", () => postTyping(false));

  refreshTypingUsers();
  window.setInterval(refreshTypingUsers, 1000);
})();
