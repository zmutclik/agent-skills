// Dimuat dari file (bukan <script> inline) agar lolos CSP script-src 'self'.
// `document`, bukan `document.body`: script di <head> berjalan sebelum <body> ada.
document.addEventListener('htmx:beforeSwap', (evt) => {
  const xhr = evt.detail.xhr;
  // 400/422: form dirender ulang dengan pesan error.
  // HX-Retarget: server sengaja mengirim fragment error (toast) untuk status lain (403/404/429/5xx).
  if (xhr.status === 400 || xhr.status === 422 || xhr.getResponseHeader('HX-Retarget')) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});
