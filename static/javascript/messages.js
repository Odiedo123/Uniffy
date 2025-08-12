// static/messages.js
document.addEventListener("DOMContentLoaded", async () => {
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

  let mentors = [];
  let selectedMentor = null;
  let messages = [];
  let lastShownTimeByUser = {};
  let selectedCardElement = null;

  const socket = io({ query: { user_id: MY_USER_ID } });

  function showToast(msg, type = "error") {
    const toast = document.createElement("div");
    toast.textContent = msg;
    toast.style.position = "fixed";
    toast.style.bottom = "20px";
    toast.style.right = "20px";
    toast.style.padding = "10px 15px";
    toast.style.borderRadius = "6px";
    toast.style.backgroundColor =
      type === "error" ? "#ff4d4f" : type === "success" ? "#52c41a" : "#1890ff";
    toast.style.color = "#fff";
    toast.style.fontSize = "14px";
    toast.style.boxShadow = "0 2px 8px rgba(0,0,0,0.15)";
    toast.style.zIndex = "9999";
    document.body.appendChild(toast);

    setTimeout(() => {
      toast.style.transition = "opacity 0.3s";
      toast.style.opacity = "0";
      setTimeout(() => toast.remove(), 300);
    }, 2000);
  }

  socket.on("connect", () => {
    console.log("socket connected", socket.id);
  });

  socket.on("new_message", (m) => {
    if (!selectedMentor) return;
    if (
      (m.sender_id === MY_USER_ID && m.receiver_id === selectedMentor.id) ||
      (m.sender_id === selectedMentor.id && m.receiver_id === MY_USER_ID)
    ) {
      const now = new Date(m.created_at).getTime();
      const lastShown = lastShownTimeByUser[m.sender_id] || 0;
      if (now - lastShown >= 4000) {
        lastShownTimeByUser[m.sender_id] = now;
        messages.push(m);
        renderMessages();
        chatWindow.scrollTop = chatWindow.scrollHeight;
      }
    }
  });

  socket.on("typing_update", (p) => {
    if (
      selectedMentor &&
      p.from_id === selectedMentor.id &&
      p.to_id === MY_USER_ID
    ) {
      showTyping(p.is_typing);
    }
  });

  socket.on("messages_seen", (p) => {
    if (selectedMentor && p.by === selectedMentor.id) {
      const el = document.getElementById("typing-indicator");
      if (el) el.remove();
    }
  });

  async function loadMentors() {
    const res = await fetch("/api/my_mentor");
    const json = await res.json();
    if (json.error) {
      mentorArea.innerText = "Error loading mentors";
      return;
    }
    mentors = json.data || [];
    if (mentors.length === 0) {
      mentorArea.innerHTML =
        '<div class="card small">No mentors assigned yet.</div>';
      chatHeader.innerText = "No mentor";
      return;
    }
    mentorArea.innerHTML = "";
    mentors.forEach(({ mentor, approved }) => {
      const mentorCard = document.createElement("div");
      mentorCard.className = "mentor-card";
      mentorCard.style.cursor = approved ? "pointer" : "not-allowed";
      mentorCard.style.padding = "8px";
      mentorCard.style.border = "1px solid #ccc";
      mentorCard.style.borderRadius = "8px";
      mentorCard.style.marginBottom = "5px";
      mentorCard.style.display = "flex";
      mentorCard.style.alignItems = "center";
      mentorCard.style.gap = "10px";
      mentorCard.innerHTML = `
        <img src="${
          mentor.avatar_url ||
          "https://ui-avatars.com/api/?name=" +
            encodeURIComponent(mentor.name || mentor.email)
        }" style="width:48px;height:48px;border-radius:50%;object-fit:cover" />
        <div>
          <div style="font-weight:600">${escape(
            mentor.name || mentor.email
          )}</div>
          <div class="small">${approved ? "Approved" : "Pending approval"}</div>
        </div>
      `;
      mentorCard.addEventListener("click", () => {
        if (approved) {
          selectMentor(mentor, mentorCard, approved);
        } else {
          showToast("This mentor has not approved the connection yet.");
        }
      });
      mentorArea.appendChild(mentorCard);
    });
  }

  async function selectMentor(mentor, cardElement, approved) {
    selectedMentor = {
      id: mentor.id,
      name: mentor.name,
      email: mentor.email,
      avatar_url: mentor.avatar_url,
      approved: approved,
    };
    chatHeader.innerText = mentor.name || mentor.email;
    await loadMessages();
    socket.emit("mark_seen", { other_id: mentor.id });

    if (selectedCardElement) {
      selectedCardElement.style.backgroundColor = "";
      selectedCardElement.style.borderColor = "#ccc";
    }
    cardElement.style.backgroundColor = "#e6f0ff";
    cardElement.style.borderColor = "#3399ff";
    selectedCardElement = cardElement;
  }

  async function loadMessages() {
    if (!selectedMentor) return;
    chatWindow.innerHTML = '<div class="small">Loading messages…</div>';
    const res = await fetch("/api/messages/" + selectedMentor.id);
    const json = await res.json();
    if (json.error) {
      chatWindow.innerHTML = `<div class="small">${escape(json.error)}</div>`;
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
        m.sender_id === MY_USER_ID ? "You" : selectedMentor.name || "M"
      )}&background=ddd" />
      <div class="bubble">
        <div class="meta"><strong>${
          m.sender_id === MY_USER_ID
            ? "You"
            : escape(selectedMentor.name || selectedMentor.email)
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
    // ✅ new logic: bypass if any card has the selected background color
    const hasSelectedCard = Array.from(
      document.querySelectorAll(".mentor-card")
    ).some((card) => card.style.backgroundColor === "rgb(230, 240, 255)");

    if (!hasSelectedCard) {
      return showToast("Please select a mentor before sending messages.");
    }

    const txt = input.value.trim();
    if (!txt) return;

    const now = new Date();
    const lastShown = lastShownTimeByUser[MY_USER_ID] || 0;

    if (now.getTime() - lastShown < 4000) {
      showToast("Please wait before sending another message.");
      return;
    }

    lastShownTimeByUser[MY_USER_ID] = now.getTime();

    const tempMessage = {
      sender_id: MY_USER_ID,
      receiver_id: selectedMentor.id,
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
        { receiver_id: selectedMentor.id, message: txt },
        (ack) => {
          if (ack && ack.error) {
            showToast("Error: " + ack.error);
          }
          input.value = "";
        }
      );
    } catch (err) {
      console.error(err);
      showToast("Network error");
    } finally {
      sendBtn.disabled = false;
    }
  }

  let typingTimeout = null;
  input.addEventListener("input", () => {
    if (!selectedMentor) return;
    socket.emit("typing", { to_id: selectedMentor.id, is_typing: true });
    clearTimeout(typingTimeout);
    typingTimeout = setTimeout(
      () =>
        socket.emit("typing", { to_id: selectedMentor.id, is_typing: false }),
      1200
    );
  });

  function showTyping(isTyping) {
    let el = document.getElementById("typing-indicator");
    if (!el && isTyping) {
      el = document.createElement("div");
      el.id = "typing-indicator";
      el.className = "typing";
      el.innerText = selectedMentor.name + " is typing...";
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

  await loadMentors();
});
