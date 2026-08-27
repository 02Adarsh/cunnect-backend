/* static/network/chat_poll.js */
(function () {
  function escapeHtml(value) {
    const element = document.createElement('div');
    element.textContent = value || '';
    return element.innerHTML;
  }

  const modal = document.getElementById('pollModal');
  const openButton = document.getElementById('pollOpen');
  const closeButton = document.getElementById('pollClose');
  const form = document.getElementById('pollForm');

  function csrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function pollHtml(poll) {
    const pollImage = poll.image_url
      ? `<img src="${escapeHtml(poll.image_url)}" class="poll-question-image" alt="Poll image">`
      : '';

    const options = (poll.options || []).map((option) => {
      const optionImage = option.image_url
        ? `<img src="${escapeHtml(option.image_url)}" class="poll-option-image" alt="${escapeHtml(option.text)}">`
        : '';

      return `
        <button type="button" data-poll-option="${option.id}" class="poll-option ${option.id === poll.selected_option_id ? 'selected' : ''}">
          <span class="poll-option-bar" style="width:${option.percentage}%"></span>
          <span class="poll-option-inner"><span class="poll-option-copy">${optionImage}<span>${escapeHtml(option.text)}</span></span><span>${option.percentage}%</span></span>
        </button>`;
    }).join('');

    return `<div class="poll-card">${pollImage}<p class="poll-question">${escapeHtml(poll.question)}</p>${options}<div class="poll-meta">${poll.total_votes} vote${poll.total_votes === 1 ? '' : 's'}</div></div>`;
  }

  function updatePoll(poll) {
    const wrapper = document.querySelector(`[data-poll-id="${poll.id}"]`);
    if (!wrapper) return;
    wrapper.dataset.voteUrl = poll.vote_url;
    wrapper.innerHTML = pollHtml(poll);
  }

  function openModal() {
    modal.classList.add('open');
    document.getElementById('pollQuestion').focus();
  }

  function closeModal() {
    modal.classList.remove('open');
    form.reset();
  }

  if (openButton) openButton.addEventListener('click', openModal);
  if (closeButton) closeButton.addEventListener('click', closeModal);
  if (modal) modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(); });

  if (form) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const question = document.getElementById('pollQuestion').value.trim();
      const options = Array.from(form.querySelectorAll('[name="options"]'))
        .map((input) => input.value.trim())
        .filter(Boolean);

      if (!question || options.length < 2) {
        alert('Enter a question and at least 2 options.');
        return;
      }

      const data = new FormData();
      data.append('question', question);
      options.forEach((option) => data.append('options[]', option));

      const pollImage = document.getElementById('pollImage');
      if (pollImage && pollImage.files[0]) {
        data.append('image', pollImage.files[0]);
      }

      form.querySelectorAll('[data-option-image]').forEach((input) => {
        const index = input.dataset.optionImage;
        if (input.files[0]) {
          data.append(`option_image_${index}`, input.files[0]);
        }
      });

      const response = await fetch(window.POLL_CREATE_URL, {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrfToken(),
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: data
      });

      const result = await response.json();
      if (result.ok) closeModal();
      else alert(result.error || 'Unable to create poll.');
    });
  }

  document.addEventListener('click', async (event) => {
    const optionButton = event.target.closest('[data-poll-option]');
    if (!optionButton) return;

    const wrapper = optionButton.closest('[data-poll-id]');
    const voteUrl = wrapper && wrapper.dataset.voteUrl;
    if (!voteUrl) return;

    const data = new URLSearchParams();
    data.append('option_id', optionButton.dataset.pollOption);

    const response = await fetch(voteUrl, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: data.toString()
    });

    const result = await response.json();
    if (result.ok) updatePoll(result.poll);
  });

  window.renderPollMessage = function (message) {
    if (!message.poll) return '';
    return `<div class="px-4 pb-3" data-poll-id="${message.poll.id}" data-vote-url="${message.poll.vote_url}">${pollHtml(message.poll)}</div>`;
  };

  window.updateRealtimePoll = updatePoll;
})();
