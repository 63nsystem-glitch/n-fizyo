// N-Fizyo – Datenschutz-Einstellungen für den eigenen Jitsi-Server der Praxis (Schweiz).
// Wird von docker-jitsi-meet an config.js angehängt (Datei: <CONFIG>/web/custom-config.js).
// Ziel: keine Verbindungen zu Drittanbietern, keine Aufzeichnung, Zutritt nur mit Schlüssel aus N-Fizyo.
config.disableThirdPartyRequests = true;          // kein Gravatar, keine externen Skripte
config.analytics = { disabled: true };
config.gravatar = { disabled: true };
config.giphy = { enabled: false };
config.p2p = Object.assign({}, config.p2p || {}, { enabled: false, stunServers: [] });   // kein Google-STUN, alles über den eigenen Server
config.fileRecordingsEnabled = false;
config.liveStreamingEnabled = false;
config.localRecording = { disable: true };
config.transcription = { enabled: false };
config.recordingService = { enabled: false };
config.disableInviteFunctions = true;             // Einladungen nur über N-Fizyo
config.enableWelcomePage = false;                 // keine freie Raumwahl auf der Startseite
config.enableClosePage = false;
config.disableDeepLinking = true;                 // im Handy-Browser bleiben, keine App nötig
config.prejoinConfig = { enabled: true, hideExtraJoinButtons: ['no-audio', 'by-phone'] };
config.requireDisplayName = false;
config.toolbarButtons = ['microphone', 'camera', 'desktop', 'chat', 'raisehand', 'tileview', 'settings', 'fullscreen', 'hangup'];
config.defaultLanguage = config.defaultLanguage || 'de';
