// Minimal Electron wrapper so the game can be packaged as a native Mac .app.
// The "TV Display" button opens a second window, placed fullscreen on an external display when one is connected.
const { app, BrowserWindow, screen } = require("electron");
const path = require("path");

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    backgroundColor: "#000321",
    title: "Jeopardy! (Host)",
  });
  win.loadFile(path.join(__dirname, "index.html"));

  win.webContents.setWindowOpenHandler(() => {
    const here = screen.getDisplayMatching(win.getBounds());
    const ext = screen.getAllDisplays().find((d) => d.id !== here.id);
    const b = (ext || here).bounds;
    return {
      action: "allow",
      overrideBrowserWindowOptions: ext
        ? { x: b.x, y: b.y, width: b.width, height: b.height, fullscreen: true, autoHideMenuBar: true, backgroundColor: "#000321" }
        : { width: 960, height: 540, autoHideMenuBar: true, backgroundColor: "#000321" },
    };
  });
}

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
