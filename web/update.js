// The control panel's update button.
// It reads "Check for updates" until a newer release is found, then
// "Update to vX". Call updateButton(button, version) once, then pass it
// state.update each time the state is fetched.
function updateButton(button, version) {
  let info = null, noteUntil = 0, note = "";

  async function post(url) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const data = await r.json();
    if (data.error) alert(data.error);
    else render(data.update);
  }

  function render(u) {
    info = u;
    version.textContent = u.current === "dev" ? "dev build" : u.current;
    button.hidden = false;
    button.disabled = false;
    button.title = "";
    button.classList.remove("error", "check");
    if (u.status === "downloading") { button.disabled = true; button.textContent = `Downloading update ${u.progress}%`; }
    else if (u.status === "installing") { button.disabled = true; button.textContent = "Installing - restarting…"; }
    else if (u.status === "error") { button.classList.add("error"); button.textContent = "Update failed - retry"; button.title = u.error || ""; }
    else if (u.available && u.can_install) { button.textContent = `⬆ Update to ${u.latest}`; button.title = u.notes || ""; }
    else {
      button.classList.add("check");
      if (u.status === "checking") { button.disabled = true; button.textContent = "Checking…"; }
      else button.textContent = Date.now() < noteUntil ? note : "Check for updates";
    }
  }

  button.onclick = async () => {
    const u = info;
    if (!u) return;
    if ((u.available && u.can_install) || u.status === "error") {
      const notes = u.notes ? `\n\nWhat's new:\n${u.notes.slice(0, 600)}` : "";
      const failed = u.status === "error" ? `The last attempt failed: ${u.error}\n\n` : "";
      if (confirm(`${failed}Install SCC Multistream ${u.latest} now?\n\nThe app will close and reopen (about 10 seconds). Any platforms that are live drop for that time, so don't update mid-stream.${notes}`))
        post("/api/update/install");
      return;
    }
    button.disabled = true;
    button.textContent = "Checking…";
    await post("/api/update/check");
    if (info && !info.available && info.status !== "error") {
      note = info.check_error ? "Couldn't check - retry" : "✓ Up to date";
      noteUntil = Date.now() + 4000;
      render(info);
      if (info.check_error) button.title = info.check_error;
      setTimeout(() => info && render(info), 4100);
    }
  };
  return render;
}
