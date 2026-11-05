# 🎮 PrimePad | Android-to-PC Virtual Gamepad

**PrimePad** is a zero-cost, ultra-low latency utility that turns your Android phone into a full Xbox 360 controller for PC gaming over local Wi-Fi. 

Powered by **WebSockets** for instant touch response and **ViGEmBus** for native Windows Xbox 360 controller emulation. Perfect for low-end PC setups, emulators, and couch gaming without extra hardware.

---

## ✨ Features

- **⚡ Low-Latency Wi-Fi Control:** Real-time touch input streaming via WebSockets.
- **🎮 Native Xbox 360 Emulation:** Powered by `vgamepad` & ViGEmBus — instantly recognized by Steam, emulators (PPSSPP, PCSX2, RPCS3), and modern PC games.
- **📳 Haptic Touch Feedback:** Tactile vibration responses for physical button feel.
- **📱 Touch Layout Engine:** Customizable touch controls built with Capacitor & Web technologies.

---

## 🛠️ Quick Installation & Setup

### 1. Windows PC Server
1. Download the latest `PrimePad v1.0.0` installer from the [Releases](https://github.com/jeremiahu02-tech/PrimePad/releases) tab.
2. Run `setup.exe` to install the background Python server and ViGEmBus driver.
3. Launch `PrimePad Server` on your PC.

### 2. Android Client
1. Install the `PrimePad.apk` from the latest release onto your phone.
2. Ensure both your PC and Android phone are on the **same Wi-Fi network**.
3. Open PrimePad on your phone, enter your PC's local IP address, or scan the qrcode from your PrimePad server on your pc and press **Connect**.

---

## 🏗️ Built With

- **Backend / Emulation:** Python, `vgamepad`, ViGEmBus, WebSockets
- **Mobile App:** HTML5, CSS3, JavaScript, Capacitor

---

## 🤝 Contributing & Feedback

Got ideas for touch layouts or latency optimizations? Open an issue or submit a pull request!

### Note:
Windows SmartScreen might show a "Publisher Unknown" warning because this is an open-source tool. Just click "Keep anyway" and "Run anyway" to install!
