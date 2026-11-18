# Digital Bridge · Website and Telegram bot

This folder contains the nurse dashboard with live ESP32 telemetry and the district-doctor Telegram bot. Start this package on its own. The repository's other backends may already use TCP 8000, UDP 41730, or the same USB serial port.

## Website

1. Run `web/start-web.bat`, then open `http://127.0.0.1:8000`.
2. For the hackathon shift, select a test profile and start the shift.
3. Choose **Sensor manbasi → Avtomatik** to discover the ESP32 or **ESP32 · faqat jonli** to require a live connection. **Demo** uses simulated readings.

## ESP32

Use the repository's [ESP32 sketch](../hardware/ai_patronaj_node/ai_patronaj_node.ino) that emits JSON on UDP 41730 or USB serial at 115200 baud. The package was tested with the packet format of the supplied AIPatronaj ESP32 sketch.

- **Wi-Fi:** Join the ESP32 access point or put both devices on the same local network. Broadcast discovery is automatic. If needed, set `ESP32_HOST` in `web/.env` to the board's IP, then restart the website. The default ESP32 access point commonly uses `192.168.4.1`.
- **USB:** Find the board's COM port and close the serial monitor. Set `ESP32_SERIAL_PORT=COM5` in `web/.env`, using your board's actual port, then restart the website.

The dashboard shows heart rate, SpO₂, temperature, actual ECG sample batches, and the MQ-135 sketch estimate and raw ADC value. This sketch does not emit respiratory rate. Live readings clear when packets stop. Use the hardware on a test bench.

## Telegram bot

1. Start the website first so it creates `web/.env`. Set your Telegram bot token in that file.
2. Restart the website to load the token. Run `telegram_bot/start-bot.bat`. On first launch it copies the website's settings to the bot's own `.env`. If the bot's file already exists, keep the token and internal key identical in both files.
3. Open the setup link printed by the bot and press **Start**. The website and bot use the same local SQLite database for user-ID pairing when run from this package.
4. Use test records only. Enable **Server** and **Onlayn** on the tablet to sync a record and receive a Telegram card.
