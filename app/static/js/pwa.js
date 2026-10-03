// PWA install helpers. UI only: nothing here touches financial data.
(function () {
  "use strict";

  // Register the service worker once the page has loaded. Failure is harmless:
  // the app works normally in a browser tab without it.
  if ("serviceWorker" in navigator) {
    var swUrl = document.body && document.body.getAttribute("data-sw-url");
    if (swUrl) {
      window.addEventListener("load", function () {
        navigator.serviceWorker.register(swUrl).catch(function () {});
      });
    }
  }

  var isStandalone = window.matchMedia && window.matchMedia("(display-mode: standalone)").matches;
  if (isStandalone || window.navigator.standalone) {
    return; // already installed: nothing to offer
  }

  // Android / desktop Chromium: offer a button when the browser says install is possible.
  var installBtn = document.getElementById("pwa-install-btn");
  var deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (installBtn) installBtn.classList.remove("d-none");
  });
  if (installBtn) {
    installBtn.addEventListener("click", function () {
      if (!deferredPrompt) return;
      deferredPrompt.prompt();
      deferredPrompt.userChoice.finally(function () {
        deferredPrompt = null;
        installBtn.classList.add("d-none");
      });
    });
  }
  window.addEventListener("appinstalled", function () {
    if (installBtn) installBtn.classList.add("d-none");
  });

  // iOS Safari has no install prompt. Show a one-time, dismissible hint instead.
  var hint = document.getElementById("pwa-ios-hint");
  var isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
  var dismissKey = "spidqah-ios-install-hint-dismissed";
  var dismissed = false;
  try { dismissed = window.localStorage.getItem(dismissKey) === "1"; } catch (e) { dismissed = false; }
  if (hint && isIos && !dismissed) {
    hint.classList.remove("d-none");
    var closeBtn = hint.querySelector("[data-pwa-dismiss]");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        hint.classList.add("d-none");
        try { window.localStorage.setItem(dismissKey, "1"); } catch (e) { /* private mode: hint may reappear */ }
      });
    }
  }
})();
