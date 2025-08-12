// static/mentor_messages.js
document.addEventListener("DOMContentLoaded", async () => {
  const SUPABASE_URL = window.SUPABASE_URL;
  const SUPABASE_ANON_KEY = window.SUPABASE_ANON_KEY;
  const MY_USER_ID = window.MY_USER_ID;

  if (!MY_USER_ID) {
    console.error("Missing MY_USER_ID");
    return;
  }

  const requestsEl = document.getElementById("requests");
  const chatWindow = document.getElementById("chat-window");
  const chatHeader = document.getElementById("chat-header");
  const input = document.getElementById("message-input");
  const sendBtn = document.getElementById("send-btn");

  let mentees = [];
  let selected = null;
  let messages = [];

  const socket = io({ query: { user_id: MY_USER_ID } });

  socket.on("connect", () => {
    console.log("socket connected", socket.id);
  });

  socket.on("connected", () => {});

  socket.on("new_message", (m) => {
    // skip own echoed messages (we already added them instantly)
    if (m.sender_id === MY_USER_ID) return;

    // if no conversation selected, just mark unread
    if (!selected) {
      markUnreadFor(m);
      return;
    }

    // ✅ Duplicate prevention: skip if same sender, same second, same text
    const createdAtSec = new Date(m.created_at).toISOString().split(".")[0];
    if (
      messages.some(
        (msg) =>
          msg.sender_id === m.sender_id &&
          msg.message === m.message &&
          new Date(msg.created_at).toISOString().split(".")[0] === createdAtSec
      )
    ) {
      return;
    }

    if (
      (m.sender_id === MY_USER_ID && m.receiver_id === selected.id) ||
      (m.sender_id === selected.id && m.receiver_id === MY_USER_ID)
    ) {
      messages.push(m);
      chatWindow.appendChild(messageEl(m));
      chatWindow.scrollTop = chatWindow.scrollHeight;
    } else {
      markUnreadFor(m);
    }
  });

  function markUnreadFor(m) {
    const otherId = m.sender_id === MY_USER_ID ? m.receiver_id : m.sender_id;
    const item = document.querySelector(`.request-item[data-id="${otherId}"]`);
    if (item) item.classList.add("has-unread");
  }

  socket.on("typing_update", (p) => {
    if (selected && p.from_id === selected.id && p.to_id === MY_USER_ID) {
      showTyping(p.is_typing);
    }
  });

  socket.on("messages_seen", (p) => {
    if (selected && p.by === selected.id) {
      const el = document.getElementById("typing-indicator");
      if (el) el.remove();
    }
  });

  async function loadRequests() {
    const res = await fetch("/api/my_requests");
    const json = await res.json();
    if (json.error) {
      requestsEl.innerText = "Error loading requests";
      return;
    }
    mentees = json.data || [];
    renderRequests();
  }

  function renderRequests() {
    requestsEl.innerHTML = "";
    mentees.forEach((rec) => {
      const s = rec.student || {};
      const el = document.createElement("div");
      el.className = "request-item";
      el.dataset.id = s.id;
      el.innerHTML = `
        <img src="${
          s.avatar_url ||
          "https://ui-avatars.com/api/?name=" +
            encodeURIComponent(s.name || s.email)
        }" style="width:44px;height:44px;border-radius:50%;object-fit:cover" />
        <div>
          <div style="font-weight:600">${escape(s.name || s.email)}</div>
          <div class="small">${rec.approved ? "Approved" : "Pending"}</div>
        </div>
        <div class="request-actions">
          ${rec.approved ? "" : '<button class="approve-btn">Approve</button>'}
        </div>
      `;
      requestsEl.appendChild(el);

      if (!rec.approved) {
        el.querySelector(".approve-btn").addEventListener(
          "click",
          async (e) => {
            e.stopPropagation();
            await approveStudent(s.id);
            rec.approved = true;
            loadRequests();
          }
        );
      }

      el.addEventListener("click", () => {
        if (rec.approved) selectMentee(s.id);
      });
    });
  }

  async function approveStudent(studentId) {
    const res = await fetch("/api/approve_student", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ student_id: studentId }),
    });
    const json = await res.json();
    if (!res.ok) {
      alert("Error approving: " + (json.error || ""));
    }
  }

  async function selectMentee(id) {
    selected = (mentees.find((m) => m.student && m.student.id === id) || {})
      .student || {
      id,
    };
    chatHeader.innerText = selected.name || selected.email || "Mentee";
    await loadMessages();
    socket.emit("mark_seen", { other_id: selected.id });
  }

  async function loadMessages() {
    if (!selected) return;
    chatWindow.innerHTML = '<div class="small">Loading messages…</div>';
    const res = await fetch("/api/messages/" + selected.id);
    const json = await res.json();
    if (json.error) {
      chatWindow.innerHTML =
        '<div class="small">' + escape(json.error) + "</div>";
      return;
    }
    messages = json.data || [];

    // ✅ Remove duplicates before rendering
    messages = messages.filter(
      (m, index, self) =>
        index ===
        self.findIndex(
          (msg) =>
            msg.sender_id === m.sender_id &&
            msg.message === m.message &&
            new Date(msg.created_at).toISOString().split(".")[0] ===
              new Date(m.created_at).toISOString().split(".")[0]
        )
    );

    renderMessages();
  }

  function renderMessages() {
    chatWindow.innerHTML = "";
    messages.forEach((m) => chatWindow.appendChild(messageEl(m)));
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }

  function messageEl(m) {
    const el = document.createElement("div");
    el.className = "msg " + (m.sender_id === MY_USER_ID ? "me" : "other");
    const avatar =
      m.sender_id === MY_USER_ID
        ? "https://ui-avatars.com/api/?name=You"
        : selected.avatar_url ||
          "https://ui-avatars.com/api/?name=" +
            encodeURIComponent(selected.name || "S");
    el.innerHTML = `
      <img class="avatar" src="${avatar}" />
      <div class="bubble">
        <div class="meta">
          <strong>${
            m.sender_id === MY_USER_ID
              ? "You"
              : escape(selected.name || selected.email)
          }</strong>
          · <span class="small">${new Date(
            m.created_at
          ).toLocaleString()}</span>
        </div>
        <div class="text">${escape(m.message)}</div>
      </div>
    `;
    return el;
  }

  sendBtn.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage();
  });

  function sendMessage() {
    if (!selected) return alert("No mentee selected");
    const text = input.value.trim();
    if (!text) return;

    sendBtn.disabled = true;

    const nowSec = new Date().toISOString().split(".")[0];

    // ✅ Prevent duplicates from being added instantly
    if (
      messages.some(
        (m) =>
          m.sender_id === MY_USER_ID &&
          m.message === text &&
          new Date(m.created_at).toISOString().split(".")[0] === nowSec
      )
    ) {
      sendBtn.disabled = false;
      input.value = "";
      return;
    }

    // show immediately
    const tempMessage = {
      sender_id: MY_USER_ID,
      receiver_id: selected.id,
      message: text,
      created_at: new Date().toISOString(),
    };
    messages.push(tempMessage);
    chatWindow.appendChild(messageEl(tempMessage));
    chatWindow.scrollTop = chatWindow.scrollHeight;

    socket.emit(
      "send_message",
      { receiver_id: selected.id, message: text },
      (ack) => {
        if (ack && ack.error) {
          alert("Error: " + ack.error);
        }
      }
    );

    input.value = "";
    sendBtn.disabled = false;
  }

  let typingTimeout = null;
  input.addEventListener("input", () => {
    if (!selected) return;
    socket.emit("typing", { to_id: selected.id, is_typing: true });
    clearTimeout(typingTimeout);
    typingTimeout = setTimeout(
      () => socket.emit("typing", { to_id: selected.id, is_typing: false }),
      1200
    );
  });

  function showTyping(isTyping) {
    let el = document.getElementById("typing-indicator");
    if (!el && isTyping) {
      el = document.createElement("div");
      el.id = "typing-indicator";
      el.className = "small typing";
      el.innerText = selected.name + " is typing...";
      chatWindow.appendChild(el);
    }
    if (!isTyping) {
      if (el) el.remove();
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

  await loadRequests();
});
