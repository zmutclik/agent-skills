// Build CSP Alpine: hanya referensi properti/method di atribut, tanpa ekspresi JS inline.
document.addEventListener('alpine:init', () => {
  Alpine.data('itemTable', () => ({
    lastSavedId: null,
    handleItemSaved(event) {
      this.lastSavedId = event.detail.id;
      this.$root.querySelector('#item-form').reset();
    },
  }));
});
