// static/messages.js
document.addEventListener("DOMContentLoaded", async () => {
  const SUPABASE_URL = window.SUPABASE_URL;
  const SUPABASE_ANON_KEY = window.SUPABASE_ANON_KEY;
  const MY_USER_ID = window.MY_USER_ID;

  if (!MY_USER_ID) {
    console.error("Missing MY_USER_ID");
    return;
  }

  const mentorArea = document.getElementById("mentor-area");
  const chatWindow = document.getElementById("chat-window");
  const input = document.getElementById("message-input");
  const sendBtn = document.getElementById("send-btn");
  const chatHeader = document.getElementById("chat-header");

  let mentor = null;
  let messages = [];
  let lastShownTimeByUser = {}; // Tracks last shown time per sender

  function getMessageKey(m) {
    return `${m.sender_id}_${Math.floor(
      new Date(m.created_at).getTime() / 1000
    )}`;
  }

  const socket = io({ query: { user_id: MY_USER_ID } });

  socket.on("connect", () => {
    console.log("socket connected", socket.id);
  });

  socket.on("new_message", (m) => {
    if (!mentor) return;

    if (
      (m.sender_id === MY_USER_ID && m.receiver_id === mentor.id) ||
      (m.sender_id === mentor.id && m.receiver_id === MY_USER_ID)
    ) {
      const now = new Date(m.created_at).getTime();
      const lastShown = lastShownTimeByUser[m.sender_id] || 0;

      // Only allow one message every 4 seconds per sender
      if (now - lastShown >= 4000) {
        lastShownTimeByUser[m.sender_id] = now;
        messages.push(m);
        renderMessages();
        chatWindow.scrollTop = chatWindow.scrollHeight;
      }
    }
  });

  socket.on("typing_update", (p) => {
    if (mentor && p.from_id === mentor.id && p.to_id === MY_USER_ID) {
      showTyping(p.is_typing);
    }
  });

  socket.on("messages_seen", (p) => {
    if (mentor && p.by === mentor.id) {
      const el = document.getElementById("typing-indicator");
      if (el) el.remove();
    }
  });

  async function loadMentor() {
    const res = await fetch("/api/my_mentor");
    const json = await res.json();
    if (json.error) {
      mentorArea.innerText = "Error loading mentor";
      return;
    }
    if (!json.data) {
      mentorArea.innerHTML =
        '<div class="card small">No mentor assigned yet.</div>';
      chatHeader.innerText = "No mentor";
      return;
    }
    mentor = json.data.mentor;
    const approved = json.data.approved;
    mentorArea.innerHTML = `
      <img src="${
        mentor.avatar_url ||
        "https://ui-avatars.com/api/?name=" +
          encodeURIComponent(mentor.name || mentor.email)
      }" style="width:64px;height:64px;border-radius:50%;object-fit:cover;float:left;margin-right:10px" />
      <div style="padding-top:8px">
        <div style="font-weight:600">${escape(
          mentor.name || mentor.email
        )}</div>
        <div class="small">${approved ? "Approved" : "Pending approval"}</div>
      </div>
      <div style="clear:both"></div>
    `;
    chatHeader.innerText = mentor.name || mentor.email;
    if (approved) {
      await loadMessages();
      socket.emit("mark_seen", { other_id: mentor.id });
    } else {
      chatWindow.innerHTML =
        '<div class="small">Waiting for mentor approval to chat.</div>';
    }
  }

  async function loadMessages() {
    if (!mentor) return;
    chatWindow.innerHTML = '<div class="small">Loading messages…</div>';
    const res = await fetch("/api/messages/" + mentor.id);
    const json = await res.json();
    if (json.error) {
      chatWindow.innerHTML =
        '<div class="small">' + escape(json.error) + "</div>";
      return;
    }

    lastShownTimeByUser = {};
    messages = [];

    (json.data || [])
      .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))
      .forEach((m) => {
        const now = new Date(m.created_at).getTime();
        const lastShown = lastShownTimeByUser[m.sender_id] || 0;
        if (now - lastShown >= 4000) {
          lastShownTimeByUser[m.sender_id] = now;
          messages.push(m);
        }
      });

    renderMessages();
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }

  function renderMessages() {
    chatWindow.innerHTML = "";
    messages.forEach((m) => chatWindow.appendChild(messageEl(m)));
  }

  function messageEl(m) {
    const el = document.createElement("div");
    el.className = "msg " + (m.sender_id === MY_USER_ID ? "me" : "other");
    el.innerHTML = `
      <img class="avatar" src="https://ui-avatars.com/api/?name=${encodeURIComponent(
        m.sender_id === MY_USER_ID ? "You" : mentor.name || "M"
      )}&background=ddd" />
      <div class="bubble">
        <div class="meta"><strong>${
          m.sender_id === MY_USER_ID
            ? "You"
            : escape(mentor.name || mentor.email)
        }</strong> · <span class="small">${new Date(
      m.created_at
    ).toLocaleString()}</span></div>
        <div class="text">${escape(m.message)}</div>
      </div>
    `;
    return el;
  }

  sendBtn.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage();
  });

  async function sendMessage() {
    if (!mentor) return alert("No mentor assigned or not approved");
    const txt = input.value.trim();
    if (!txt) return;

    const now = new Date();
    const lastShown = lastShownTimeByUser[MY_USER_ID] || 0;

    // Prevent sending multiple messages within 4 seconds
    if (now.getTime() - lastShown < 4000) {
      alert("Please wait before sending another message.");
      return;
    }

    lastShownTimeByUser[MY_USER_ID] = now.getTime();

    const tempMessage = {
      sender_id: MY_USER_ID,
      receiver_id: mentor.id,
      message: txt,
      created_at: now.toISOString(),
    };

    messages.push(tempMessage);
    renderMessages();
    chatWindow.scrollTop = chatWindow.scrollHeight;

    sendBtn.disabled = true;
    try {
      socket.emit(
        "send_message",
        { receiver_id: mentor.id, message: txt },
        (ack) => {
          if (ack && ack.error) {
            alert("Error: " + ack.error);
          }
          input.value = "";
        }
      );
    } catch (err) {
      console.error(err);
      alert("Network error");
    } finally {
      sendBtn.disabled = false;
    }
  }

  let typingTimeout = null;
  input.addEventListener("input", () => {
    if (!mentor) return;
    socket.emit("typing", { to_id: mentor.id, is_typing: true });
    clearTimeout(typingTimeout);
    typingTimeout = setTimeout(
      () => socket.emit("typing", { to_id: mentor.id, is_typing: false }),
      1200
    );
  });

  function showTyping(isTyping) {
    let el = document.getElementById("typing-indicator");
    if (!el && isTyping) {
      el = document.createElement("div");
      el.id = "typing-indicator";
      el.className = "typing";
      el.innerText = mentor.name + " is typing...";
      chatWindow.appendChild(el);
    }
    if (!isTyping && el) {
      el.remove();
    }
  }

  function escape(s) {
    if (!s) return "";
    return String(s).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }[c])
    );
  }

  await loadMentor();
});
