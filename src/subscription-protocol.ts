// Selected fields from probe.subscriptions; no inference prompt was sent.
export const subscriptionProtocol = {
  "source": "test/subscriptions-probe-2026-10-06.json",
  "toolVersion": "2.1.289 (Claude Code)",
  "status": {
    "loggedIn": false,
    "authMethod": "none",
    "apiProvider": "firstParty",
    "analyticsDisabled": false,
    "projectsDirectory": "/var/folders/2q/l86hj4950s3468xgx0s9rv2h0000gn/T/syl78-probe-Gg5UKo/fresh/projects",
    "configDirectory": "/var/folders/2q/l86hj4950s3468xgx0s9rv2h0000gn/T/syl78-probe-Gg5UKo/fresh"
  },
  "initialize": {
    "type": "control_response",
    "response": {
      "subtype": "success",
      "request_id": "probe",
      "response": {
        "models": [
          {
            "value": "default",
            "resolvedModel": "claude-opus-5-5",
            "displayName": "Default (recommended)",
            "description": "Use the default model (currently Opus 5.5) · $4/$20 per Mtok",
            "supportsEffort": true,
            "supportedEffortLevels": [
              "low",
              "medium",
              "high",
              "xhigh",
              "max"
            ],
            "supportsAdaptiveThinking": true,
            "supportsFastMode": true,
            "supportsAutoMode": true
          },
          {
            "value": "opus",
            "resolvedModel": "claude-opus-5-5",
            "displayName": "Opus",
            "description": "Opus 5.5 · Best for everyday, complex tasks · $4/$20 per Mtok",
            "supportsEffort": true,
            "supportedEffortLevels": [
              "low",
              "medium",
              "high",
              "xhigh",
              "max"
            ],
            "supportsAdaptiveThinking": true,
            "supportsFastMode": true,
            "supportsAutoMode": true
          },
          {
            "value": "fable",
            "resolvedModel": "claude-fable-5-1",
            "displayName": "Fable",
            "description": "Fable 5.1 · Most capable for your hardest and longest-running tasks · $10/$50 per Mtok",
            "supportsEffort": true,
            "supportedEffortLevels": [
              "low",
              "medium",
              "high",
              "xhigh",
              "max"
            ],
            "supportsAdaptiveThinking": true,
            "supportsAutoMode": true
          },
          {
            "value": "sonnet",
            "resolvedModel": "claude-sonnet-5-5",
            "displayName": "Sonnet",
            "description": "Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok",
            "supportsEffort": true,
            "supportedEffortLevels": [
              "low",
              "medium",
              "high",
              "xhigh",
              "max"
            ],
            "supportsAdaptiveThinking": true,
            "supportsAutoMode": true
          },
          {
            "value": "haiku",
            "resolvedModel": "claude-haiku-4-5-20251001",
            "displayName": "Haiku",
            "description": "Haiku 4.5 · Fastest for quick answers · $1/$5 per Mtok"
          }
        ]
      }
    }
  }
};
