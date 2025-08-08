const fileInput = document.getElementById("fileInput");
const profilePic = document.getElementById("profilePic");
const form = document.getElementById("uploadForm");

fileInput.addEventListener("change", function () {
  const file = this.files[0];
  if (file && file.type.startsWith("image/")) {
    const reader = new FileReader();
    reader.onload = function (e) {
      profilePic.style.backgroundImage = `url('${e.target.result}')`;
      // Automatically submit the form when image is selected: form.submit();
    };
    reader.readAsDataURL(file);
  }
});

function updateFileName(input) {
  const fileName = input.files[0]?.name || "";
  document.getElementById("fileName").textContent = fileName;
}

function setProfileImage(input) {
  const file = input.files[0];
  if (file) {
    const reader = new FileReader();
    reader.onload = function (e) {
      const profilePic = document.getElementById("profile-pic");
      profilePic.style.backgroundImage = `url('${e.target.result}')`;
      profilePic.classList.add("filled");
    };
    reader.readAsDataURL(file);
  }
}

function updateFileName(input) {
  const fileName = input.files[0]?.name || "";
  document.getElementById("fileName").textContent = fileName;
}
