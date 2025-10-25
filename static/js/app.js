
document.addEventListener('DOMContentLoaded', () => {
  // Confirm deletion buttons
  document.querySelectorAll('[data-confirm]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      if (!confirm(btn.getAttribute('data-confirm'))) {
        e.preventDefault();
      }
    });
  });

  // Require selects (e.g., item/category must be chosen)
  document.querySelectorAll('form').forEach(f => {
    f.addEventListener('submit', (e) => {
      const reqSelects = f.querySelectorAll('select[required]');
      for (const s of reqSelects) {
        if (!s.value) {
          e.preventDefault();
          alert('กรุณาเลือกตัวเลือกที่จำเป็นให้ครบก่อนบันทึก');
          s.focus();
          break;
        }
      }
    });
  });
});
