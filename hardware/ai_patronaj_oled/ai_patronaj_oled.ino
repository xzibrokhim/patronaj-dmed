#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <U8g2lib.h>
#include <math.h>
// ================= WI-FI DUAL TRANSPORT (AP + STA) =================
#define WIFI_UDP_PORT     41730
#define WIFI_CMD_UDP_PORT 41731
const char* AP_SSID = "AIPatronaj-BioNode";
const char* AP_PASS = "12345678";
struct WifiCred {
  const char* ssid;
  const char* pass;
};
const WifiCred WIFI_CREDS[] = {
  {"YOUR_2.4GHZ_WIFI_SSID", "YOUR_2.4GHZ_WIFI_PASSWORD"},
  {"AIPatronaj-Ground", "12345678"}
};
const int WIFI_CRED_COUNT = sizeof(WIFI_CREDS) / sizeof(WIFI_CREDS[0]);
WiFiUDP udp;
WiFiUDP cmdUdp;
WebServer httpServer(80);
int wifiCredIdx = 0;
unsigned long wifiAttemptStartMs = 0;
unsigned long lastWifiCheckMs = 0;
bool wifiWasConnected = false;
char latestJson[768] = "{\"status\":\"BOOT\",\"zone\":\"GREEN\"}";
// ================= PINOUT (ESP32-S3-N16R8 HW GPIO) =================
// I2C Bus (GY-MAX30102 + OLED 0.96"/1.3")
#define I2C_SDA     8
#define I2C_SCL     9
// MQ-135 Gas / Breath / CO Sensor (VCC -> 5Vin, GND -> GND)
#define MQ135_PIN   1   // ADC1_CH0 (AO pin)
// AD8232 ECG Sensor (3.3V -> 3V3, GND -> GND)
#define ECG_OUT_PIN 2   // ADC1_CH1 (OUTPUT pin)
#define ECG_LO_MIN  6   // LO- pin
#define ECG_LO_PLUS 7   // LO+ pin
// DS18B20 Waterproof Steel Temp Probe (Red -> 3V3, Black -> GND, Yellow -> GPIO 5)
#define DS18B20_PIN 5   // Self-contained 1-Wire (4.7k pullup to 3V3 recommended)
// Optional Output / Control
#define BUZZER_PIN  10  // Optional piezo buzzer
#define START_BTN   11  // Optional physical START button (active LOW)
#define RGB_PIN     48  // Onboard WS2812 RGB LED on ESP32-S3-N16R8
// ================= OBJECTS =================
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);
// ================= SENSOR FLAGS =================
bool hasOLED = false;
bool hasMAX  = false;
bool hasDS18 = false;
// ================= MAX30102 STATE (HR & SpO2) =================
uint32_t lastIrVal = 0;
uint32_t lastRedVal = 0;
bool fingerOnSensor = false;
float irDC = 0.0f;
float redDC = 0.0f;
float irLP = 0.0f;
float irPrev = 0.0f;
float irPrevPrev = 0.0f;
float irMinWin = 1e9f, irMaxWin = -1e9f;
float redMinWin = 1e9f, redMaxWin = -1e9f;
unsigned long lastAcWindow = 0;
unsigned long lastBeatMs = 0;
const int RATE_BUF_SIZE = 4;
float rateBuf[RATE_BUF_SIZE] = {72, 72, 72, 72};
int rateIdx = 0;
int beatAvg = 0;
int spo2Val = 0;
// ================= AD8232 ECG STATE =================
#define ECG_BATCH_SIZE 16
int ecgWave[ECG_BATCH_SIZE];
int ecgIdx = 0;
int ecgMvLatest = 0;
bool ecgLeadsOk = false;
unsigned long lastEcgSampleMs = 0;
// ================= DS18B20 BODY TEMP STATE =================
float bodyTemp = 36.6f;
unsigned long lastDsConvertMs = 0;
bool dsConverting = false;
// ================= MQ-135 GAS / BREATH STATE =================
int mqRaw = 0;
int mqBaseline = 0;
int co2Ppm = 420;
bool beatPulseDot = false;
// ================= TRIAGE ZONE FEEDBACK =================
String currentZone = "GREEN";
bool alarmBeepActive = false;
unsigned long lastAlarmPulseMs = 0;
unsigned long lastTelemetryMs = 0;
String i2cSummary = "";
String serialRxBuf = "";
// ================= BUZZER & RGB LED HELPERS =================
void triggerBeep(uint16_t durationMs, uint16_t freqHz = 2400) {
  tone(BUZZER_PIN, freqHz, durationMs);
}
void applyRiskZoneLED(const String& zone, bool beep) {
  String z = zone;
  z.trim();
  z.toUpperCase();
  if (z != "RED" && z != "YELLOW" && z != "GREEN") return;
  currentZone = z;
  if (z == "RED") {
    rgbLedWrite(RGB_PIN, 64, 0, 0);
    alarmBeepActive = beep;
    if (beep) triggerBeep(220, 2800);
  } else if (z == "YELLOW") {
    rgbLedWrite(RGB_PIN, 56, 42, 0);
    alarmBeepActive = false;
    if (beep) triggerBeep(100, 1800);
  } else {
    rgbLedWrite(RGB_PIN, 0, 56, 0);
    alarmBeepActive = false;
    if (beep) triggerBeep(60, 1400);
  }
}
void parseCommandPayload(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;
  if (cmd.startsWith("ZONE:")) {
    int firstColon = cmd.indexOf(':');
    int secondColon = cmd.indexOf(':', firstColon + 1);
    String zone = (secondColon > 0)
      ? cmd.substring(firstColon + 1, secondColon)
      : cmd.substring(firstColon + 1);
    bool beep = (secondColon > 0) ? (cmd.substring(secondColon + 1).toInt() != 0) : true;
    applyRiskZoneLED(zone, beep);
  } else if (cmd.startsWith("{")) {
    bool beep = (cmd.indexOf("\"beep\":false") < 0);
    if (cmd.indexOf("RED") >= 0) applyRiskZoneLED("RED", beep);
    else if (cmd.indexOf("YELLOW") >= 0) applyRiskZoneLED("YELLOW", beep);
    else if (cmd.indexOf("GREEN") >= 0) applyRiskZoneLED("GREEN", beep);
  }
}
void pollIncomingCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialRxBuf.length() > 0) {
        parseCommandPayload(serialRxBuf);
        serialRxBuf = "";
      }
    } else if (serialRxBuf.length() < 160) {
      serialRxBuf += c;
    }
  }
  int packetSize = cmdUdp.parsePacket();
  if (packetSize > 0) {
    char buf[160];
    int n = cmdUdp.read(buf, sizeof(buf) - 1);
    if (n > 0) {
      buf[n] = '\0';
      parseCommandPayload(String(buf));
    }
  }
  if (alarmBeepActive && currentZone == "RED") {
    unsigned long now = millis();
    if (now - lastAlarmPulseMs > 1200) {
      lastAlarmPulseMs = now;
      triggerBeep(120, 2900);
    }
  }
}
// ================= SELF-CONTAINED 1-WIRE FOR DS18B20 =================
bool oneWireReset(uint8_t pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(480);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(70);
  bool presence = (digitalRead(pin) == LOW);
  delayMicroseconds(410);
  return presence;
}
void oneWireWriteBit(uint8_t pin, uint8_t bitVal) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  if (bitVal) {
    delayMicroseconds(6);
    pinMode(pin, INPUT_PULLUP);
    delayMicroseconds(64);
  } else {
    delayMicroseconds(60);
    pinMode(pin, INPUT_PULLUP);
    delayMicroseconds(10);
  }
}
uint8_t oneWireReadBit(uint8_t pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(6);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(9);
  uint8_t r = (uint8_t)digitalRead(pin);
  delayMicroseconds(55);
  return r;
}
void oneWireWriteByte(uint8_t pin, uint8_t v) {
  for (int i = 0; i < 8; i++) {
    oneWireWriteBit(pin, (v >> i) & 0x01);
  }
}
uint8_t oneWireReadByte(uint8_t pin) {
  uint8_t v = 0;
  for (int i = 0; i < 8; i++) {
    if (oneWireReadBit(pin)) v |= (1 << i);
  }
  return v;
}
void pollDS18B20() {
  unsigned long now = millis();
  if (!dsConverting) {
    if (!oneWireReset(DS18B20_PIN)) {
      hasDS18 = false;
      return;
    }
    hasDS18 = true;
    oneWireWriteByte(DS18B20_PIN, 0xCC);
    oneWireWriteByte(DS18B20_PIN, 0x44);
    dsConverting = true;
    lastDsConvertMs = now;
  } else if (now - lastDsConvertMs >= 800) {
    dsConverting = false;
    if (!oneWireReset(DS18B20_PIN)) return;
    oneWireWriteByte(DS18B20_PIN, 0xCC);
    oneWireWriteByte(DS18B20_PIN, 0xBE);
    uint8_t data[9];
    for (int i = 0; i < 9; i++) data[i] = oneWireReadByte(DS18B20_PIN);
    int16_t raw = (int16_t)((data[1] << 8) | data[0]);
    float c = raw / 16.0f;
    if (c > 10.0f && c < 50.0f) {
      bodyTemp = c;
    }
  }
}
// ================= AD8232 ECG SAMPLER =================
void pollAD8232() {
  unsigned long now = millis();
  if (now - lastEcgSampleMs < 20) return; // 50 Hz waveform sampling
  lastEcgSampleMs = now;
  bool loPlus = (digitalRead(ECG_LO_PLUS) == HIGH);
  bool loMin  = (digitalRead(ECG_LO_MIN)  == HIGH);
  ecgLeadsOk = (!loPlus && !loMin);
  int raw = analogRead(ECG_OUT_PIN);
  int mv = (int)((raw * 3300L) / 4095L);
  ecgMvLatest = mv;
  ecgWave[ecgIdx] = ecgLeadsOk ? raw : 2048;
  ecgIdx = (ecgIdx + 1) % ECG_BATCH_SIZE;
}
// ================= I2C HELPERS & MAX30102 =================
bool i2cPing(uint8_t addr) {
  Wire.beginTransmission(addr);
  return (Wire.endTransmission() == 0);
}
void i2cWriteReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}
uint8_t i2cReadReg(uint8_t addr, uint8_t reg) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return 0;
  Wire.requestFrom(addr, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}
void initMAX30102() {
  if (!i2cPing(0x57)) return;
  i2cWriteReg(0x57, 0x09, 0x40);
  delay(60);
  i2cWriteReg(0x57, 0x02, 0x00);
  i2cWriteReg(0x57, 0x03, 0x00);
  i2cWriteReg(0x57, 0x04, 0x00);
  i2cWriteReg(0x57, 0x05, 0x00);
  i2cWriteReg(0x57, 0x06, 0x00);
  i2cWriteReg(0x57, 0x08, 0x4F);
  i2cWriteReg(0x57, 0x09, 0x03);
  i2cWriteReg(0x57, 0x0A, 0x27);
  i2cWriteReg(0x57, 0x0C, 0x24);
  i2cWriteReg(0x57, 0x0D, 0x24);
  hasMAX = true;
}
void pollMAX30102() {
  if (!hasMAX) return;
  uint8_t wr = i2cReadReg(0x57, 0x04) & 0x1F;
  uint8_t rd = i2cReadReg(0x57, 0x06) & 0x1F;
  int samples = (wr - rd) & 0x1F;
  if (samples == 0) return;
  if (samples > 16) samples = 16;
  for (int i = 0; i < samples; i++) {
    Wire.beginTransmission(0x57);
    Wire.write(0x07);
    if (Wire.endTransmission(false) != 0) break;
    if (Wire.requestFrom((uint8_t)0x57, (uint8_t)6) != 6) break;
    uint32_t red = ((uint32_t)Wire.read() << 16) | ((uint32_t)Wire.read() << 8) | Wire.read();
    uint32_t ir  = ((uint32_t)Wire.read() << 16) | ((uint32_t)Wire.read() << 8) | Wire.read();
    red &= 0x03FFFF;
    ir  &= 0x03FFFF;
    lastRedVal = red;
    lastIrVal  = ir;
    if (ir > 30000) {
      if (!fingerOnSensor) {
        fingerOnSensor = true;
        irDC = (float)ir;
        redDC = (float)red;
        irLP = 0.0f;
        irPrev = 0.0f;
        irPrevPrev = 0.0f;
        lastBeatMs = millis();
        beatAvg = 74;
        spo2Val = 98;
      }
      irDC  = irDC * 0.95f + (float)ir * 0.05f;
      redDC = redDC * 0.95f + (float)red * 0.05f;
      float irAC  = (float)ir - irDC;
      float redAC = (float)red - redDC;
      irLP = irLP * 0.65f + irAC * 0.35f;
      if (irAC < irMinWin) irMinWin = irAC;
      if (irAC > irMaxWin) irMaxWin = irAC;
      if (redAC < redMinWin) redMinWin = redAC;
      if (redAC > redMaxWin) redMaxWin = redAC;
      if (irPrev > irPrevPrev && irPrev > irLP && irPrev > 35.0f) {
        unsigned long now = millis();
        unsigned long dt = now - lastBeatMs;
        if (dt >= 350 && dt <= 1600) {
          float bpm = 60000.0f / (float)dt;
          rateBuf[rateIdx] = bpm;
          rateIdx = (rateIdx + 1) % RATE_BUF_SIZE;
          float sum = 0;
          for (int k = 0; k < RATE_BUF_SIZE; k++) sum += rateBuf[k];
          beatAvg = (int)roundf(sum / RATE_BUF_SIZE);
          lastBeatMs = now;
          if (currentZone != "RED") triggerBeep(22, 2200);
        } else if (dt > 1600) {
          lastBeatMs = now;
        }
      }
      irPrevPrev = irPrev;
      irPrev = irLP;
      if (millis() - lastAcWindow > 1500) {
        float irPP = irMaxWin - irMinWin;
        float redPP = redMaxWin - redMinWin;
        if (irPP > 50.0f && irDC > 1000.0f && redDC > 1000.0f) {
          float R = (redPP / redDC) / (irPP / irDC);
          int calcSpo2 = (int)roundf(104.0f - 17.0f * R);
          spo2Val = constrain(calcSpo2, 88, 99);
        }
        irMinWin = 1e9f; irMaxWin = -1e9f;
        redMinWin = 1e9f; redMaxWin = -1e9f;
        lastAcWindow = millis();
      }
    } else {
      fingerOnSensor = false;
      beatAvg = 0;
      spo2Val = 0;
    }
  }
}
// ================= OLED DRAW (AI-PATRONAJ ECG + VITALS) =================
void drawOLED(const char* pillStatus) {
  if (!hasOLED) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.setDrawColor(1);
  u8g2.setCursor(1, 9);
  u8g2.print("AI-PATRONAJ");
  if (WiFi.status() == WL_CONNECTED) {
    u8g2.setFont(u8g2_font_5x7_tr);
    u8g2.setCursor(69, 8);
    u8g2.print("W");
    u8g2.setFont(u8g2_font_6x10_tr);
  }
  u8g2.drawBox(76, 0, 52, 11);
  u8g2.setDrawColor(0);
  int statusLen = strlen(pillStatus);
  int statusX = 76 + max(0, (52 - statusLen * 6) / 2);
  u8g2.setCursor(statusX, 9);
  u8g2.print(pillStatus);
  u8g2.setDrawColor(1);
  u8g2.drawRFrame(0, 13, 62, 26, 2);
  u8g2.setFont(u8g2_font_5x7_tr);
  u8g2.setCursor(4, 21);
  u8g2.print("PULS bpm");
  if (fingerOnSensor && beatPulseDot) u8g2.drawDisc(55, 18, 2);
  beatPulseDot = !beatPulseDot;
  u8g2.setFont(u8g2_font_7x14B_tr);
  u8g2.setCursor(4, 36);
  if (!hasMAX) u8g2.print("ERR");
  else if (fingerOnSensor) u8g2.print(beatAvg);
  else u8g2.print("--");
  u8g2.drawRFrame(66, 13, 62, 26, 2);
  u8g2.setFont(u8g2_font_5x7_tr);
  u8g2.setCursor(70, 21);
  u8g2.print("SpO2 %");
  if (!hasMAX) {
    u8g2.setFont(u8g2_font_7x14B_tr);
    u8g2.setCursor(70, 36);
    u8g2.print("ERR");
  } else if (fingerOnSensor) {
    u8g2.setFont(u8g2_font_7x14B_tr);
    u8g2.setCursor(70, 36);
    u8g2.print(spo2Val);
    u8g2.print("%");
  } else {
    u8g2.setFont(u8g2_font_6x10_tr);
    u8g2.setCursor(70, 34);
    u8g2.print("BARMOQ");
  }
  if (currentZone == "RED") {
    u8g2.drawBox(0, 41, 128, 23);
    u8g2.setDrawColor(0);
    u8g2.setFont(u8g2_font_6x10_tr);
    u8g2.setCursor(16, 51);
    u8g2.print("!! QIZIL ZONA !!");
    u8g2.setFont(u8g2_font_5x7_tr);
    u8g2.setCursor(8, 60);
    u8g2.print("EKG & SpO2 -> SHIFOKOR");
    u8g2.setDrawColor(1);
  } else {
    u8g2.setFont(u8g2_font_6x10_tr);
    u8g2.setCursor(1, 50);
    u8g2.print("T:");
    if (hasDS18) {
      u8g2.print(bodyTemp, 1);
      u8g2.print("C");
    } else {
      u8g2.print("--.-C");
    }
    u8g2.setCursor(58, 50);
    u8g2.print(ecgLeadsOk ? "EKG:OK" : "EKG:OFF");
    u8g2.setCursor(1, 62);
    u8g2.print("MQ:");
    u8g2.print(co2Ppm);
    u8g2.print("ppm");
    u8g2.setCursor(78, 62);
    u8g2.print("Z:");
    u8g2.print(currentZone);
  }
  u8g2.sendBuffer();
}
void scanI2C() {
  int found = 0;
  i2cSummary = "";
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      if (found > 0) i2cSummary += " ";
      i2cSummary += "0x" + String(addr, HEX);
      found++;
    }
  }
  if (found == 0) i2cSummary = "NONE";
}
void startWifiAttempt(int idx) {
  wifiCredIdx = idx % WIFI_CRED_COUNT;
  wifiAttemptStartMs = millis();
  WiFi.disconnect(false);
  WiFi.begin(WIFI_CREDS[wifiCredIdx].ssid, WIFI_CREDS[wifiCredIdx].pass);
}
void initWifiAndServer() {
  WiFi.mode(WIFI_AP_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(false);
  WiFi.softAP(AP_SSID, AP_PASS);
  httpServer.on("/api/hardware", HTTP_GET, []() {
    httpServer.sendHeader("Access-Control-Allow-Origin", "*");
    httpServer.send(200, "application/json", latestJson);
  });
  httpServer.on("/zone", HTTP_POST, []() {
    httpServer.sendHeader("Access-Control-Allow-Origin", "*");
    if (httpServer.hasArg("plain")) parseCommandPayload(httpServer.arg("plain"));
    httpServer.send(200, "application/json", "{\"ok\":true}");
  });
  httpServer.begin();
  udp.begin(WIFI_UDP_PORT);
  cmdUdp.begin(WIFI_CMD_UDP_PORT);
  startWifiAttempt(0);
}
void maintainWifiNonBlocking() {
  unsigned long now = millis();
  if (now - lastWifiCheckMs < 400) return;
  lastWifiCheckMs = now;
  bool connected = (WiFi.status() == WL_CONNECTED);
  if (connected && !wifiWasConnected) {
    wifiWasConnected = true;
  } else if (!connected) {
    if (wifiWasConnected) {
      wifiWasConnected = false;
      startWifiAttempt(wifiCredIdx);
    } else if (now - wifiAttemptStartMs >= 9000) {
      startWifiAttempt((wifiCredIdx + 1) % WIFI_CRED_COUNT);
    }
  }
}
void sendUdpTo(const IPAddress& ip, const char* payload, size_t len) {
  if (udp.beginPacket(ip, WIFI_UDP_PORT)) {
    udp.write(reinterpret_cast<const uint8_t*>(payload), len);
    udp.endPacket();
  }
}
void broadcastTelemetryUdp(const char* payload, size_t len) {
  sendUdpTo(IPAddress(255, 255, 255, 255), payload, len);
  sendUdpTo(IPAddress(192, 168, 4, 255), payload, len);
  if (WiFi.status() == WL_CONNECTED) {
    IPAddress gw = WiFi.gatewayIP();
    if ((uint32_t)gw != 0) sendUdpTo(gw, payload, len);
    IPAddress bcast((uint32_t)WiFi.localIP() | ~(uint32_t)WiFi.subnetMask());
    if ((uint32_t)bcast != 0) sendUdpTo(bcast, payload, len);
  }
}
void setup() {
  Serial.begin(115200);
  delay(400);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  pinMode(START_BTN, INPUT_PULLUP);
  pinMode(ECG_LO_MIN, INPUT);
  pinMode(ECG_LO_PLUS, INPUT);
  analogReadResolution(12);
  pinMode(ECG_OUT_PIN, INPUT);
  pinMode(MQ135_PIN, INPUT);
  for (int i = 0; i < ECG_BATCH_SIZE; i++) ecgWave[i] = 2048;
  applyRiskZoneLED("GREEN", false);
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);
  Wire.setTimeOut(50);
  scanI2C();
  if (i2cPing(0x3C)) {
    hasOLED = true;
    u8g2.setBusClock(100000);
    u8g2.begin();
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_6x10_tr);
    u8g2.setCursor(4, 18); u8g2.print("AI-PATRONAJ ECG");
    u8g2.setCursor(4, 34); u8g2.print("MAX30102+AD8232");
    u8g2.setCursor(4, 52); u8g2.print("I2C: " + i2cSummary.substring(0, 15));
    u8g2.sendBuffer();
    delay(700);
  }
  initMAX30102();
  long mqSum = 0;
  for (int i = 0; i < 16; i++) {
    mqSum += analogRead(MQ135_PIN);
    delay(10);
  }
  mqBaseline = (int)(mqSum / 16);
  initWifiAndServer();
  triggerBeep(60, 2000);
}
void loop() {
  pollIncomingCommands();
  pollAD8232();
  pollMAX30102();
  pollDS18B20();
  maintainWifiNonBlocking();
  httpServer.handleClient();
  unsigned long now = millis();
  if (now - lastTelemetryMs >= 320) {
    lastTelemetryMs = now;
    mqRaw = analogRead(MQ135_PIN);
    int mqDiff = abs(mqRaw - mqBaseline);
    co2Ppm = 415 + constrain(mqDiff * 2, 0, 1800);
    bool buttonStart = (digitalRead(START_BTN) == LOW);
    const char* pill = currentZone.c_str();
    if (!fingerOnSensor && !ecgLeadsOk && currentZone == "GREEN") pill = "OFFLINE";
    drawOLED(pill);
    char waveStr[140];
    int pos = 0;
    waveStr[pos++] = '[';
    for (int i = 0; i < ECG_BATCH_SIZE; i++) {
      int idx = (ecgIdx + i) % ECG_BATCH_SIZE;
      pos += snprintf(waveStr + pos, sizeof(waveStr) - pos, "%s%d", (i > 0 ? "," : ""), ecgWave[idx]);
    }
    waveStr[pos++] = ']';
    waveStr[pos] = '\0';
    bool wifiSta = (WiFi.status() == WL_CONNECTED);
    String ipStr = wifiSta ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
    int len = snprintf(
      latestJson,
      sizeof(latestJson),
      "{\"hr\":%d,\"spo2\":%d,\"temp\":%.1f,\"temp_ok\":%s,\"ecg_mv\":%d,\"ecg_leads_ok\":%s,\"ecg_wave\":%s,\"co2\":%d,\"mq_raw\":%d,\"finger\":%s,\"button_start\":%s,\"zone\":\"%s\",\"wifi\":%s,\"ip\":\"%s\",\"status\":\"%s\"}",
      fingerOnSensor ? beatAvg : 0,
      fingerOnSensor ? spo2Val : 0,
      hasDS18 ? bodyTemp : 0.0f,
      hasDS18 ? "true" : "false",
      ecgMvLatest,
      ecgLeadsOk ? "true" : "false",
      waveStr,
      co2Ppm,
      mqRaw,
      fingerOnSensor ? "true" : "false",
      buttonStart ? "true" : "false",
      currentZone.c_str(),
      wifiSta ? "true" : "false",
      ipStr.c_str(),
      pill
    );
    if (len > 0 && len < (int)sizeof(latestJson)) {
      Serial.println(latestJson);
      broadcastTelemetryUdp(latestJson, (size_t)len);
    }
  }
}
