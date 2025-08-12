document.addEventListener("DOMContentLoaded", () => {
  fetch("/api/courses")
    .then((res) => res.json())
    .then((data) => {
      const grid = document.querySelector(".course-grid");
      grid.innerHTML = ""; // Clear static cards

      data.data.forEach((course) => {
        const card = document.createElement("div");
        card.classList.add("course-card");

        card.innerHTML = `
                    <img src="${
                      course.image_url || "static/img/coding.jpg"
                    }" class="course-img" alt="${course.course_name}" />
                    <h2 class="course-title">${course.course_name}</h2>
                    <p class="course-desc">${course.description || ""}</p>
                    <progress value="0" max="100"></progress>
                    <button class="course-btn">Try it Out</button>
                `;

        card.querySelector(".course-btn").addEventListener("click", () => {
          fetch("/api/assign_mentor", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ course_name: course.course_name }),
          })
            .then((res) => res.json())
            .then((resp) => {
              if (resp.mentor_id) {
                window.location.href = `/messages`;
              } else {
                alert(resp.error || "Something went wrong");
              }
            });
        });

        grid.appendChild(card);
      });
    });
});
