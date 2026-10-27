// Where the séance room's API lives.
// Locally (python -m seance) and on the Render service, the page and the API share an origin, so this stays empty.
// On the static Vercel site it points at the Render service.
(function () {
  const host = location.hostname;
  const sameOrigin = host === "127.0.0.1" || host === "localhost" || host.endsWith(".onrender.com");
  window.SEANCE_API = sameOrigin ? "" : "https://git-seance.onrender.com";

  // Séances recorded on a laptop with a local model, replayed on the public site. Never shown in local mode.
  window.SEANCE_REPLAYS = [{
    key: "amazon-ml",
    src: "/static/replays/amazon-ml.json",
    tomb: {
      name: "amazon-ml", status: "dead", born: "2026-09-25T12:31:00+05:30", died: "2026-09-27T16:28:00+05:30",
      last_words: "Model v3, judge/validation notebooks, gitattributes for model files",
      cause_hint: "The hackathon ended", successor: { name: "plain-words" },
    },
  }];
})();
