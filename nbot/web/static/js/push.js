(function () {
  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; i += 1) {
      outputArray[i] = rawData.charCodeAt(i);
    }

    return outputArray;
  }

  async function getRegistration() {
    const registration = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    return registration;
  }

  async function enableNekoPush(sessionId = "") {
    if (!window.isSecureContext) {
      throw new Error("Browser notifications require HTTPS or localhost.");
    }
    if (!("serviceWorker" in navigator)) {
      throw new Error("Current browser does not support Service Worker.");
    }
    if (!("PushManager" in window)) {
      throw new Error("Current browser does not support Web Push.");
    }
    if (!("Notification" in window)) {
      throw new Error("Current browser does not support notifications.");
    }

    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      throw new Error("Notification permission was not granted.");
    }

    const registration = await getRegistration();
    const keyResp = await fetch("/api/push/public-key", {
      credentials: "include",
      cache: "no-store",
    });
    const { publicKey } = await keyResp.json();
    if (!publicKey) {
      throw new Error("Server did not provide a Web Push public key.");
    }

    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
    }

    const resp = await fetch("/api/push/subscribe", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId || "",
        subscription: subscription.toJSON(),
      }),
    });
    if (!resp.ok) {
      throw new Error("Failed to save notification subscription.");
    }
    return subscription;
  }

  async function disableNekoPush() {
    if (!("serviceWorker" in navigator)) return true;
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return true;

    const endpoint = subscription.endpoint;
    await subscription.unsubscribe();
    await fetch("/api/push/unsubscribe", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }),
    });
    return true;
  }

  async function getNekoPushState() {
    const secureContext = !!window.isSecureContext;
    const supported =
      secureContext &&
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window;
    if (!supported) {
      return {
        supported: false,
        permission: "unsupported",
        subscribed: false,
        secureContext,
      };
    }

    let subscribed = false;
    try {
      const registration = await getRegistration();
      subscribed = !!(await registration.pushManager.getSubscription());
    } catch {
      subscribed = false;
    }

    return {
      supported: true,
      permission: Notification.permission,
      subscribed,
      secureContext,
    };
  }

  window.NekoPush = {
    enable: enableNekoPush,
    disable: disableNekoPush,
    state: getNekoPushState,
  };
})();
