/*
  MICROCORE (MCX) ARDUINO UNO MINER
  No WiFi - uses Serial to communicate with computer bridge
  Username-based | Auto failover via bridge
*/

#include <ArduinoJson.h>
#include <EEPROM.h>

// ==================== USER CONFIGURATION ====================
const char* USERNAME = "your_username";
const char* PRIVATE_KEY = "your_32_byte_private_key_here";
uint32_t INITIAL_STAKE = 100;

// ==================== CONSTANTS ====================
#define SYMBOL "MCX"
#define LEVEL_STAKE_RANGE 100
#define SIGNING_WINDOW_MS 2500
#define SLASH_RATE 0.10
#define UPTIME_PING_INTERVAL 30000

#define EEPROM_STAKE_ADDR 0
#define EEPROM_REWARDS_ADDR 4
#define EEPROM_BLOCKS_ADDR 8
#define EEPROM_UPTIME_ADDR 12
#define EEPROM_CHECKSUM_ADDR 16

// ==================== GLOBAL VARIABLES ====================
uint32_t currentStake;
uint32_t totalRewards;
uint32_t totalBlocksSigned;
uint32_t uptimeSeconds;
uint32_t currentLevel;
uint32_t lastUptimePing;
uint32_t lastChallengeTime;
uint32_t uptimeCounter;
uint32_t consecutiveMisses;
uint32_t slashCount;
uint32_t currentBlockId;

char validatorID[65];
char walletAddress[70];
char currentChallenge[65];
bool isValidator = false;
bool isRegistered = false;
String incomingData = "";

// ==================== SIMPLE CRYPTO ====================
void computeSHA256(const char* input, char* output) {
    unsigned long hash = 5381;
    for (int i = 0; input[i] != '\0'; i++) hash = ((hash << 5) + hash) + input[i];
    sprintf(output, "%016lx", hash);
}

void generateValidatorID() {
    char combined[100];
    snprintf(combined, sizeof(combined), "%s%s", USERNAME, PRIVATE_KEY);
    computeSHA256(combined, validatorID);
    computeSHA256(validatorID, walletAddress);
    char temp[65];
    strcpy(temp, walletAddress);
    snprintf(walletAddress, sizeof(walletAddress), "MCR_%.16s", temp);
}

// ==================== STAKING & LEVELS ====================
void calculateLevel() {
    currentLevel = ((currentStake - 1) / LEVEL_STAKE_RANGE) + 1;
    if (currentLevel < 1) currentLevel = 1;
    if (currentLevel > 100) currentLevel = 100;
}

uint32_t computeChecksum() {
    uint32_t sum = currentStake + totalRewards + totalBlocksSigned + uptimeSeconds + slashCount;
    return sum ^ 0x5A5A5A5A;
}

void saveToEEPROM() {
    EEPROM.begin(512);
    EEPROM.put(EEPROM_STAKE_ADDR, currentStake);
    EEPROM.put(EEPROM_REWARDS_ADDR, totalRewards);
    EEPROM.put(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
    EEPROM.put(EEPROM_UPTIME_ADDR, uptimeSeconds);
    uint32_t checksum = computeChecksum();
    EEPROM.put(EEPROM_CHECKSUM_ADDR, checksum);
    EEPROM.commit();
    EEPROM.end();
}

void loadFromEEPROM() {
    EEPROM.begin(512);
    EEPROM.get(EEPROM_STAKE_ADDR, currentStake);
    EEPROM.get(EEPROM_REWARDS_ADDR, totalRewards);
    EEPROM.get(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
    EEPROM.get(EEPROM_UPTIME_ADDR, uptimeSeconds);
    uint32_t storedChecksum;
    EEPROM.get(EEPROM_CHECKSUM_ADDR, storedChecksum);
    EEPROM.end();
    uint32_t calculatedChecksum = computeChecksum();
    if (currentStake < 100 || currentStake > 10000000 || storedChecksum != calculatedChecksum) {
        currentStake = INITIAL_STAKE;
        totalRewards = 0;
        totalBlocksSigned = 0;
        uptimeSeconds = 0;
        slashCount = 0;
        calculateLevel();
        saveToEEPROM();
    }
    calculateLevel();
}

// ==================== SLASHING & REWARDS ====================
void handleSlashing() {
    uint32_t slashAmount = currentStake * SLASH_RATE;
    if (slashAmount < LEVEL_STAKE_RANGE) slashAmount = LEVEL_STAKE_RANGE;
    currentStake -= slashAmount;
    if (currentStake < LEVEL_STAKE_RANGE) currentStake = LEVEL_STAKE_RANGE;
    slashCount++;
    calculateLevel();
    saveToEEPROM();
    consecutiveMisses++;
    Serial.print("[SLASH] Lost "); Serial.print(slashAmount); Serial.print(" MCX | Stake: ");
    Serial.print(currentStake); Serial.print(" | Level: "); Serial.println(currentLevel);
}

void addReward(uint32_t rewardAmount) {
    totalRewards += rewardAmount;
    currentStake += rewardAmount;
    totalBlocksSigned++;
    consecutiveMisses = 0;
    calculateLevel();
    saveToEEPROM();
    Serial.print("[REWARD] +"); Serial.print(rewardAmount); Serial.print(" MCX | Total: ");
    Serial.print(totalRewards); Serial.print(" | Stake: "); Serial.print(currentStake);
    Serial.print(" | Level: "); Serial.println(currentLevel);
}

// ==================== COMMUNICATION WITH BRIDGE ====================
void sendToBridge(String json) { Serial.println(json); }

void sendRegister() {
    StaticJsonDocument<512> doc;
    doc["type"] = "register";
    doc["validator_id"] = validatorID;
    doc["username"] = USERNAME;
    doc["wallet"] = walletAddress;
    doc["stake"] = currentStake;
    doc["level"] = currentLevel;
    doc["rewards"] = totalRewards;
    doc["blocks"] = totalBlocksSigned;
    doc["timestamp"] = millis() / 1000;
    char messageToSign[100];
    snprintf(messageToSign, sizeof(messageToSign), "%s%s%lu", validatorID, USERNAME, currentStake);
    char signature[33];
    computeSHA256(messageToSign, signature);
    doc["signature"] = signature;
    String output;
    serializeJson(doc, output);
    sendToBridge(output);
    Serial.println("[REG] Sent");
}

void sendUptimePing() {
    StaticJsonDocument<256> doc;
    doc["type"] = "uptime_ping";
    doc["validator_id"] = validatorID;
    doc["username"] = USERNAME;
    doc["uptime_seconds"] = uptimeCounter;
    doc["stake"] = currentStake;
    doc["level"] = currentLevel;
    String output;
    serializeJson(doc, output);
    sendToBridge(output);
}

void sendBlockSignature() {
    char messageToSign[100];
    snprintf(messageToSign, sizeof(messageToSign), "%s%s%lu", currentChallenge, validatorID, currentBlockId);
    char signature[33];
    computeSHA256(messageToSign, signature);
    StaticJsonDocument<512> doc;
    doc["type"] = "block_signature";
    doc["validator_id"] = validatorID;
    doc["username"] = USERNAME;
    doc["challenge"] = currentChallenge;
    doc["signature"] = signature;
    doc["level"] = currentLevel;
    doc["stake"] = currentStake;
    doc["block_id"] = currentBlockId;
    String output;
    serializeJson(doc, output);
    sendToBridge(output);
    Serial.println("[SIGN] Sent");
}

void processMessage(String msg) {
    StaticJsonDocument<1024> doc;
    DeserializationError error = deserializeJson(doc, msg);
    if (error) return;
    const char* type = doc["type"];
    if (strcmp(type, "registered") == 0) {
        isRegistered = true;
        Serial.print("[REGISTERED] Level: "); Serial.println(doc["level"].as<int>());
    } else if (strcmp(type, "challenge") == 0) {
        strncpy(currentChallenge, doc["challenge"], 64);
        currentBlockId = doc["block_id"];
        lastChallengeTime = millis();
        isValidator = true;
        sendBlockSignature();
        Serial.println("[CHALLENGE] Received");
    } else if (strcmp(type, "block_accepted") == 0) {
        addReward(doc["reward"]);
        isValidator = false;
        Serial.print("[ACCEPT] Block "); Serial.println(doc["block_id"].as<uint32_t>());
    } else if (strcmp(type, "block_rejected") == 0) {
        isValidator = false;
        Serial.println("[REJECT] Block rejected");
    } else if (strcmp(type, "slash") == 0) { handleSlashing(); isValidator = false; }
}

// ==================== SETUP & LOOP ====================
void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n=== MICROCORE ARDUINO UNO MINER ===");
    loadFromEEPROM();
    generateValidatorID();
    calculateLevel();
    Serial.print("Username: "); Serial.println(USERNAME);
    Serial.print("Wallet: "); Serial.println(walletAddress);
    Serial.print("Stake: "); Serial.print(currentStake); Serial.print(" MCX, Level: "); Serial.println(currentLevel);
    sendRegister();
    lastUptimePing = millis();
    uptimeCounter = 0;
    isValidator = false;
    Serial.println("\n[READY]");
}

void loop() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') { if (incomingData.length() > 0) { processMessage(incomingData); incomingData = ""; } }
        else { incomingData += c; }
    }
    if (millis() - lastUptimePing >= UPTIME_PING_INTERVAL) {
        uptimeCounter++; sendUptimePing(); lastUptimePing = millis();
        if (uptimeCounter % 2 == 0) {
            Serial.print("[STATUS] Stake: "); Serial.print(currentStake); Serial.print(" MCX, Level: ");
            Serial.print(currentLevel); Serial.print(", Blocks: "); Serial.print(totalBlocksSigned);
            Serial.print(", Rewards: "); Serial.println(totalRewards);
        }
    }
    if (isValidator && (millis() - lastChallengeTime >= SIGNING_WINDOW_MS)) {
        Serial.println("[TIMEOUT]"); handleSlashing(); isValidator = false;
    }
    static uint32_t lastSave = 0;
    if (millis() - lastSave >= 3600000) { saveToEEPROM(); lastSave = millis(); }
    delay(10);
}
