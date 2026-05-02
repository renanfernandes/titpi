# Project Context: Raspberry Pi 5 AI Camera (IMX500)

## Hardware Environment
- **Device:** Raspberry Pi Zero 2w
- **Camera:** Sony IMX500 (AI Camera).
- **OS:** Raspberry Pi OS (Bookworm) 64-bit.

## Technical Constraints
- **Picamera2/IMX500:** Use the `picamera2.devices.IMX500` module for AI metadata. Standard `load_post_processing` calls on the camera object will fail.
- **Latency:** Prioritize `ultrafast` presets and `zerolatency` tuning for any streaming tasks.

## Code Standards
- Use `Picamera2` for Python scripts.
- Prefer `ffmpeg` pipes (`|`) for network streaming to ensure stability.
- Always include `request.release()` in loops to prevent memory leaks.