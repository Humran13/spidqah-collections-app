(function () {
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const dateInput = document.getElementById('entry-date');
  const typeInputs = document.querySelectorAll('input[name="collection_type_choice"]');
  const contributorInput = document.getElementById('contributor-name');
  const contributorIdInput = document.getElementById('contributor-id');
  const suggestions = document.getElementById('contributor-suggestions');
  const amountInput = document.getElementById('amount');
  const noteInput = document.getElementById('note');
  const form = document.getElementById('entry-form');
  const messageBox = document.getElementById('entry-message');
  const confirmBox = document.getElementById('confirm-box');
  const confirmText = document.getElementById('confirm-text');
  const confirmExistingBtn = document.getElementById('confirm-use-existing');
  const confirmNewBtn = document.getElementById('confirm-add-new');

  let searchTimer = null;
  let pendingConfirmName = null;
  let pendingSimilar = [];

  function currentType() {
    const checked = document.querySelector('input[name="collection_type_choice"]:checked');
    return checked ? checked.value : 'MUKULULO';
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function updateTotals(totals) {
    // Full detail card (always present) and the compact mobile-only
    // glanceable strip (m-total-*, only rendered below lg) both mirror
    // the same figures - update whichever elements exist.
    setText('total-mukululo', totals.MUKULULO_fmt);
    setText('total-friday', totals.FRIDAY_fmt);
    setText('total-sunday', totals.SUNDAY_fmt);
    setText('total-overall', totals.OVERALL_fmt);
    setText('m-total-mukululo', totals.MUKULULO_fmt);
    setText('m-total-friday', totals.FRIDAY_fmt);
    setText('m-total-sunday', totals.SUNDAY_fmt);
    setText('m-total-overall', totals.OVERALL_fmt);
  }

  function fmtUGX(n) {
    return 'UGX ' + Number(n).toLocaleString('en-US');
  }

  function formatTotals(raw) {
    return {
      MUKULULO_fmt: fmtUGX(raw.MUKULULO),
      FRIDAY_fmt: fmtUGX(raw.FRIDAY),
      SUNDAY_fmt: fmtUGX(raw.SUNDAY),
      OVERALL_fmt: fmtUGX(raw.OVERALL),
    };
  }

  contributorInput.addEventListener('input', function () {
    contributorIdInput.value = '';
    const q = contributorInput.value.trim();
    clearTimeout(searchTimer);
    if (q.length < 2) {
      suggestions.innerHTML = '';
      suggestions.classList.add('d-none');
      return;
    }
    searchTimer = setTimeout(function () {
      fetch('/collections/api/contributor-search?q=' + encodeURIComponent(q))
        .then(r => r.json())
        .then(list => {
          suggestions.innerHTML = '';
          if (!list.length) {
            suggestions.classList.add('d-none');
            return;
          }
          list.forEach(c => {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'list-group-item list-group-item-action';
            item.textContent = c.name;
            item.addEventListener('click', function () {
              contributorInput.value = c.name;
              contributorIdInput.value = c.id;
              suggestions.innerHTML = '';
              suggestions.classList.add('d-none');
              amountInput.focus();
            });
            suggestions.appendChild(item);
          });
          suggestions.classList.remove('d-none');
        });
    }, 200);
  });

  document.addEventListener('click', function (e) {
    if (!suggestions.contains(e.target) && e.target !== contributorInput) {
      suggestions.classList.add('d-none');
    }
  });

  function submitEntry(extra) {
    const payload = Object.assign({
      date: dateInput.value,
      collection_type: currentType(),
      contributor_id: contributorIdInput.value || null,
      contributor_name: contributorInput.value.trim(),
      amount: amountInput.value,
      note: noteInput.value,
    }, extra || {});

    messageBox.innerHTML = '';
    confirmBox.classList.add('d-none');

    fetch('/collections/api/save-entry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
      body: JSON.stringify(payload),
    })
      .then(async r => ({ status: r.status, data: await r.json() }))
      .then(({ status, data }) => {
        if (status === 200 && data.ok) {
          messageBox.innerHTML = '<div class="alert alert-success py-2">Saved: ' + data.contributor.name + ' - ' + fmtUGX(payload.amount) + '</div>';
          updateTotals(formatTotals(data.totals));
          contributorInput.value = '';
          contributorIdInput.value = '';
          amountInput.value = '';
          noteInput.value = '';
          contributorInput.focus();
        } else if (status === 409 && data.needs_confirmation) {
          pendingConfirmName = contributorInput.value.trim();
          pendingSimilar = data.similar;
          confirmText.textContent = data.message;
          confirmBox.classList.remove('d-none');
        } else {
          messageBox.innerHTML = '<div class="alert alert-danger py-2">' + (data.error || 'Could not save entry.') + '</div>';
        }
      })
      .catch(() => {
        messageBox.innerHTML = '<div class="alert alert-danger py-2">Network error - please try again.</div>';
      });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (!contributorInput.value.trim()) {
      messageBox.innerHTML = '<div class="alert alert-danger py-2">Enter a contributor name.</div>';
      return;
    }
    if (!amountInput.value) {
      messageBox.innerHTML = '<div class="alert alert-danger py-2">Enter an amount.</div>';
      return;
    }
    submitEntry({});
  });

  confirmExistingBtn.addEventListener('click', function () {
    if (pendingSimilar.length) {
      submitEntry({ confirm_use_existing_id: pendingSimilar[0].id });
    }
  });

  confirmNewBtn.addEventListener('click', function () {
    submitEntry({ confirm_new: true });
  });

  typeInputs.forEach(el => el.addEventListener('change', function () {
    const url = new URL(window.location);
    url.searchParams.set('type', currentType());
    url.searchParams.set('date', dateInput.value);
    window.location = url;
  }));

  dateInput.addEventListener('change', function () {
    const url = new URL(window.location);
    url.searchParams.set('date', dateInput.value);
    url.searchParams.set('type', currentType());
    window.location = url;
  });
})();
