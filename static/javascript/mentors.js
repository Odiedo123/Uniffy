document.addEventListener("DOMContentLoaded", async () => {
  const grid = document.getElementById("mentorGrid");

  async function fetchMentors() {
    try {
      const res = await fetch("/api/university_verified_users");
      const data = await res.json();

      if (!data.data || !Array.isArray(data.data) || data.data.length === 0) {
        grid.innerHTML = "<p>No mentors available at the moment.</p>";
        return;
      }

      grid.innerHTML = ""; // Clear old mentors

      data.data.forEach((mentor) => {
        const card = document.createElement("div");
        card.className = "mentor-card";

        const profileImg = mentor.profile_image || "/static/img/blackgirl.png";

        // Join courses array into a string or show "No courses"
        const courseText =
          mentor.courses && mentor.courses.length > 0
            ? mentor.courses.join(", ")
            : "No courses";

        card.innerHTML = `
            <div class="img-container">
              <img src="${profileImg}" alt="${mentor.name}" class="mentor-img">
            </div>
            <h3 class="mentor-name">${mentor.name}</h3>
            <p class="mentor-courses">${courseText}</p>
            <button class="mentor-btn" data-id="${mentor.id}">Message</button>
          `;

        grid.appendChild(card);
      });

      // Handle message button clicks
      document.querySelectorAll(".mentor-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const mentorId = btn.dataset.id;

          try {
            const approveRes = await fetch("/api/request_mentor", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ mentor_id: mentorId }),
            });

            const approveData = await approveRes.json();
            if (approveData.ok) {
              window.location.href = `/messages`;
            } else {
              alert(approveData.error || "Error approving mentor.");
            }
          } catch (err) {
            console.error("Error approving mentor:", err);
            alert("Something went wrong. Please try again.");
          }
        });
      });
    } catch (err) {
      console.error("Error fetching mentors:", err);
      if (grid) grid.innerHTML = "<p>Failed to load mentors.</p>";
    }
  }

  if (grid) {
    fetchMentors();
  } else {
    console.error("mentorGrid element not found in DOM.");
  }
});
