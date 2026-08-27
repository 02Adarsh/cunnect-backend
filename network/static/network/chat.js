/* static/network/chat.js - CUnnect real-time chat */
(function () {
  'use strict';

  const CURRENT_USER = window.CURRENT_USER || { name: 'You', initial: 'Y' };
  const CURRENT_USERNAME = window.CURRENT_USER_USERNAME || '';
  let socket = null;
  let selectedImage = null;
  let selectedVideo = null;
  let selectedAttachment = null;
  let typingTimer = null;
  const typingUsers = new Map();
  let lightboxScale = 1;
  let lightboxOffsetX = 0;
  let lightboxOffsetY = 0;
  let pinchStartDistance = 0;
  let pinchStartScale = 1;
  let panStartX = 0;
  let panStartY = 0;
  let panOriginX = 0;
  let panOriginY = 0;
  let lastImageTap = 0;

  function applyLightboxTransform() {
    const image = document.getElementById('image-lightbox-image');
    if (!image) return;

    image.style.transform = `translate(${lightboxOffsetX}px, ${lightboxOffsetY}px) scale(${lightboxScale})`;
  }

  function resetLightboxZoom() {
    lightboxScale = 1;
    lightboxOffsetX = 0;
    lightboxOffsetY = 0;
    applyLightboxTransform();
  }

  function touchDistance(touchOne, touchTwo) {
    return Math.hypot(
      touchTwo.clientX - touchOne.clientX,
      touchTwo.clientY - touchOne.clientY
    );
  }

  function installRealtimeLinkStyle() {
    if (document.getElementById('cunnect-realtime-link-style')) return;

    const style = document.createElement('style');
    style.id = 'cunnect-realtime-link-style';
    style.textContent = `
      .chat-link {
        color: #E8000D !important;
        font-weight: 700;
        text-decoration: underline;
        text-decoration-color: rgba(232, 0, 13, .48);
        text-underline-offset: 3px;
        overflow-wrap: anywhere;
      }
      .chat-link:hover { color: #ff6973 !important; }
    `;
    document.head.appendChild(style);
  }

  function getCookie(name) {
    const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function csrfToken() {
    return getCookie('csrftoken') ||
      (document.querySelector('[name="csrfmiddlewaretoken"]') || {}).value || '';
  }

  function escapeHtml(value) {
    const element = document.createElement('div');
    element.textContent = value || '';
    return element.innerHTML;
  }

  // Converts safe text URLs to red, clickable external links.
  function linkifyText(value) {
    const escapedText = escapeHtml(value).replace(/\n/g, '<br>');

    return escapedText.replace(
      /((?:https?:\/\/|www\.)[^\s<]+)/gi,
      (matchedUrl) => {
        const href = matchedUrl.startsWith('www.')
          ? `https://${matchedUrl}`
          : matchedUrl;

        return `<a class="chat-link" href="${href}" target="_blank" rel="noopener noreferrer" style="color:#E8000D !important;font-weight:700;text-decoration:underline;text-decoration-color:rgba(232,0,13,.48);text-underline-offset:3px;overflow-wrap:anywhere;">${matchedUrl}</a>`;
      }
    );
  }

  function relativeTime(dateValue) {
    if (!dateValue) return "just now";

    const date = new Date(dateValue);
    if (Number.isNaN(date.getTime())) return "just now";

    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return "just now";

    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;

    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hr ago`;

    const days = Math.floor(hours / 24);
    if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;

    return date.toLocaleDateString("en-IN", {
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  }

  function openImageLightbox(imageUrl, imageAlt) {
    const lightbox = document.getElementById('image-lightbox');
    const image = document.getElementById('image-lightbox-image');
    if (!lightbox || !image || !imageUrl) return;

    resetLightboxZoom();
    image.src = imageUrl;
    image.alt = imageAlt || 'Full screen image';
    lightbox.classList.add('open');
    lightbox.setAttribute('aria-hidden', 'false');
  }

  function closeImageLightbox() {
    const lightbox = document.getElementById('image-lightbox');
    const image = document.getElementById('image-lightbox-image');
    if (!lightbox || !image) return;

    lightbox.classList.remove('open');
    lightbox.setAttribute('aria-hidden', 'true');
    resetLightboxZoom();
    image.src = '';
  }

  function getFeed() {
    return document.getElementById('feed-container');
  }

  function scrollToBottom() {
    const feed = getFeed();
    if (feed) feed.scrollTop = feed.scrollHeight;
  }

  function openAtLatestMessage() {
    const feed = getFeed();
    if (!feed) return;

    const moveToLatest = () => {
      feed.scrollTop = feed.scrollHeight;
    };

    // Initial render + delayed media layout changes.
    requestAnimationFrame(moveToLatest);
    window.setTimeout(moveToLatest, 150);
    window.setTimeout(moveToLatest, 700);

    feed.querySelectorAll('img, video').forEach((media) => {
      media.addEventListener('load', moveToLatest, { once: true });
      media.addEventListener('loadedmetadata', moveToLatest, { once: true });
    });
  }

  function messageMediaHtml(message) {
    let html = '';

    if (message.image_url) {
      html += `
        <div class="message-bubble media-frame">
          <img src="${escapeHtml(message.image_url)}" alt="Chat image" data-zoom-image>
        </div>`;
    }

    if (message.video_url) {
      html += `
        <div class="message-bubble media-frame">
          <video controls playsinline preload="metadata">
            <source src="${escapeHtml(message.video_url)}" type="video/mp4">
          </video>
        </div>`;
    }

    if (message.audio_url) {
      html += `
        <div class="message-bubble">
          <audio class="chat-audio" controls preload="metadata">
            <source src="${escapeHtml(message.audio_url)}">
          </audio>
        </div>`;
    }

    if (message.attachment_url) {
      html += `
        <div class="message-bubble attachment-row">
          <span class="file-icon">▤</span>
          <p>${escapeHtml(message.attachment_name || 'Attachment')}</p>
          <a href="${escapeHtml(message.attachment_url)}" download class="attachment-tag">FILE</a>
        </div>`;
    }

    return html;
  }

  function replyMediaHtml(message) {
    let html = '';

    if (message.image_url) {
      html += `<img src="${escapeHtml(message.image_url)}" class="chat-image-zoom" alt="Reply image" data-zoom-image>`;
    }

    if (message.video_url) {
      html += `<video controls playsinline preload="metadata"><source src="${escapeHtml(message.video_url)}" type="video/mp4"></video>`;
    }

    if (message.audio_url) {
      html += `<audio class="chat-audio" controls preload="metadata"><source src="${escapeHtml(message.audio_url)}"></audio>`;
    }

    if (message.attachment_url) {
      html += `<a href="${escapeHtml(message.attachment_url)}" download class="chat-link">📎 ${escapeHtml(message.attachment_name || 'Attachment')}</a>`;
    }

    return html;
  }

  function messageCardHtml(message) {
    const fullName = message.full_name || message.username || 'Unknown';
    const initial = (fullName[0] || 'U').toUpperCase();
    const isOwn = message.username === CURRENT_USERNAME;
    const avatar = message.profile_photo_url
      ? `<img src="${escapeHtml(message.profile_photo_url)}" alt="${escapeHtml(fullName)}">`
      : escapeHtml(initial);
    const content = message.content
      ? (isOwn
          ? `<div class="message-bubble"><p class="message-text" data-message-content>${linkifyText(message.content)}</p></div>`
          : `<div class="message-bubble"><p class="message-text" data-message-content>${linkifyText(message.content)}</p></div>`)
      : '';
    const ownerActions = isOwn
      ? `<button data-edit-message data-edit-url="${escapeHtml(message.edit_url || '')}" type="button">✎</button><button data-delete-message data-delete-url="${escapeHtml(message.delete_url || '')}" type="button">×</button>`
      : '';
    const pinAction = window.IS_ROOM_ADMIN
      ? `<button data-pin-message type="button" class="${message.is_pinned ? 'reacted' : ''}">⌖</button>`
      : '';
    const poll = window.renderPollMessage ? window.renderPollMessage(message) : '';

    return `
      <div class="chat-avatar ${isOwn ? 'own-avatar' : ''}">${avatar}</div>
      <div class="body">
        <div class="head"><span class="who">${escapeHtml(fullName)}</span><span class="meta" data-message-time data-course="${escapeHtml(message.course || 'Campus Member')}" data-created-at="${escapeHtml(message.created_at_iso || '')}">${escapeHtml(message.course || 'Campus Member')} · ${relativeTime(message.created_at_iso)}</span></div>
        ${content}
        ${messageMediaHtml(message)}
        ${poll}
        <div class="message-actions">
          <button data-react-btn type="button" class="${message.user_emoji ? 'reacted' : ''}">♥ <span data-react-icon>${escapeHtml(message.user_emoji || '')}</span><span data-react-count>${message.count || message.reaction_count || 0}</span></button>
          <button data-reply-btn type="button">◌ Reply</button>
          ${pinAction}
          ${ownerActions}
        </div>
        <div class="hidden" data-emoji-picker>
          <button data-emoji="❤️">❤️</button><button data-emoji="😂">😂</button><button data-emoji="🔥">🔥</button><button data-emoji="😍">😍</button><button data-emoji="👍">👍</button>
        </div>
        <div class="hidden" data-replies-list data-expanded="false"><div class="reply-content"></div></div>
        <form class="reply-inline hidden" data-reply-inline enctype="multipart/form-data">
          <input type="text" name="content" placeholder="Write a reply..." required>
          <input type="file" name="image" data-reply-image>
          <input type="file" name="video" data-reply-video>
          <input type="file" name="attachment" data-reply-attachment>
          <button type="button" data-cancel-reply>Cancel</button><button type="submit">Reply</button>
        </form>
      </div>`;
  }

  function appendPost(message) {
    if (!message || document.querySelector(`[data-message-id="${message.id}"]`)) return;

    const feed = getFeed();
    if (!feed) return;

    const empty = document.getElementById('empty-state');
    if (empty) empty.remove();

    const article = document.createElement('article');
    article.className = `post-card msg ${message.username === CURRENT_USERNAME ? 'own' : ''}`;
    article.dataset.messageId = message.id;
    article.dataset.likeUrl = message.like_url || '';
    article.dataset.pinUrl = message.pin_url || '';
    article.dataset.isPinned = message.is_pinned ? 'true' : 'false';
    article.innerHTML = messageCardHtml(message);

    const anchor = document.getElementById('feed-anchor');
    if (anchor && anchor.parentNode === feed) feed.insertBefore(article, anchor);
    else feed.appendChild(article);

    scrollToBottom();
  }

  function appendReply(message) {
    const parent = document.querySelector(`[data-message-id="${message.parent_id}"]`);
    if (!parent) return;

    const list = parent.querySelector('[data-replies-list]');
    const content = parent.querySelector('.reply-content');
    if (!list || !content) return;

    list.classList.remove('hidden');
    const fullName = message.full_name || message.username || 'Unknown';
    const initial = (fullName[0] || 'U').toUpperCase();
    const avatar = message.profile_photo_url
      ? `<img src="${escapeHtml(message.profile_photo_url)}" alt="${escapeHtml(fullName)}">`
      : escapeHtml(initial);

    const reply = document.createElement('div');
    reply.className = 'reply-item';
    reply.dataset.replyItem = '';
    reply.innerHTML = `<b>${escapeHtml(fullName)}</b>${message.content ? `<p class="linkified-content">${linkifyText(message.content)}</p>` : ''}${replyMediaHtml(message)}<small data-message-time data-course="${escapeHtml(message.course || 'Campus Member')}" data-created-at="${escapeHtml(message.created_at_iso || '')}">${escapeHtml(message.course || 'Campus Member')} · ${relativeTime(message.created_at_iso)}</small>`;
    content.appendChild(reply);
  }

  function safeRealtimeMessageHtml(message) {
    const fullName = message.full_name || message.username || 'Unknown';
    const initial = (fullName[0] || 'U').toUpperCase();
    const isOwn = message.username === CURRENT_USERNAME;
    const avatar = message.profile_photo_url
      ? `<img src="${escapeHtml(message.profile_photo_url)}" alt="${escapeHtml(fullName)}">`
      : escapeHtml(initial);
    const content = message.is_deleted
      ? '🚫 This message was deleted'
      : (message.content || '');
    const media = message.is_deleted ? '' : messageMediaHtml(message);
    const poll = (
      !message.is_deleted && window.renderPollMessage
    ) ? window.renderPollMessage(message) : '';

    return `
      <div class="chat-avatar ${isOwn ? 'own-avatar' : ''}">${avatar}</div>
      <div class="body">
        <div class="head">
          <span class="who">${escapeHtml(fullName)}</span>
          <span class="meta" data-message-time data-course="${escapeHtml(message.course || 'Campus Member')}" data-created-at="${escapeHtml(message.created_at_iso || '')}">${escapeHtml(message.course || 'Campus Member')} · ${relativeTime(message.created_at_iso)}</span>
        </div>
        <div class="message-bubble"><p class="message-text ${message.is_deleted ? 'deleted-copy' : ''}" data-message-content>${linkifyText(content)}</p></div>
        ${media}
        ${poll}
        <div class="message-actions">
          <button data-react-btn type="button">♥ <span data-react-icon>${escapeHtml(message.user_emoji || '')}</span><span data-react-count>${message.count || 0}</span></button>
          <button data-reply-btn type="button">◌ Reply</button>
          <button data-forward-message data-forward-url="${escapeHtml(message.forward_url || '')}" type="button">↗</button>
          <button data-star-message data-star-url="${escapeHtml(message.star_url || '')}" type="button" class="${message.is_starred ? 'wa-starred' : ''}">★</button>
          ${window.IS_ROOM_ADMIN ? `<button data-pin-message type="button">⌖</button>` : ''}
          ${isOwn ? `<button data-edit-message data-edit-url="${escapeHtml(message.edit_url || '')}" type="button">✎</button><button data-delete-message data-delete-url="${escapeHtml(message.delete_url || '')}" type="button">×</button>` : ''}
        </div>
        <div class="hidden" data-emoji-picker><button data-emoji="❤️">❤️</button><button data-emoji="😂">😂</button><button data-emoji="🔥">🔥</button><button data-emoji="👍">👍</button></div>
        <div class="hidden" data-replies-list data-expanded="false"><div class="reply-content"></div></div>
        <form class="reply-inline hidden" data-reply-inline enctype="multipart/form-data"><input name="content" placeholder="Write a reply..."><input type="file" name="image" data-reply-image><input type="file" name="video" data-reply-video><input type="file" name="attachment" data-reply-attachment><button type="button" data-cancel-reply>Cancel</button><button type="submit">Reply</button></form>
      </div>`;
  }

  function appendSecondUiMessage(message) {
    if (!message || document.querySelector(`[data-message-id="${message.id}"]`)) return;
    const feed = getFeed();
    if (!feed) return;

    const empty = document.getElementById('empty-state');
    if (empty) empty.remove();

    const card = document.createElement('article');
    card.className = `post-card msg ${message.username === CURRENT_USERNAME ? 'own' : ''}`;
    card.dataset.messageId = message.id;
    card.dataset.likeUrl = message.like_url || '';
    card.dataset.pinUrl = message.pin_url || '';
    card.dataset.isPinned = message.is_pinned ? 'true' : 'false';
    card.innerHTML = safeRealtimeMessageHtml(message);

    const anchor = document.getElementById('feed-anchor');
    if (anchor && anchor.parentNode === feed) feed.insertBefore(card, anchor);
    else feed.appendChild(card);
    scrollToBottom();
  }

  function renderIncomingMessage(message) {
    if (!message) return;
    if (message.parent_id) {
      appendReply(message);
      return;
    }
    appendSecondUiMessage(message);
  }

  function showFilePreview() {
    const file = selectedImage || selectedVideo || selectedAttachment;
    const preview = document.getElementById('file-preview');
    const previewName = document.getElementById('file-preview-name');

    if (!preview) return;

    if (file) {
      preview.classList.remove('hidden');
      preview.style.display = 'flex';
      if (previewName) previewName.textContent = `${file.name} (${Math.round(file.size / 1024)} KB)`;
    } else {
      preview.classList.add('hidden');
      preview.style.display = 'none';
    }
  }

  function clearFiles() {
    selectedImage = null;
    selectedVideo = null;
    selectedAttachment = null;
    ['#media-input', '#video-input', '#file-input'].forEach((selector) => {
      const input = document.querySelector(selector);
      if (input) input.value = '';
    });
    showFilePreview();
  }

  function bindFileInput(selector, type) {
    const input = document.querySelector(selector);
    if (!input) return;

    input.addEventListener('change', () => {
      const file = input.files[0] || null;
      const imageInput = document.querySelector('#media-input');
      const videoInput = document.querySelector('#video-input');
      const attachmentInput = document.querySelector('#file-input');

      selectedImage = null;
      selectedVideo = null;
      selectedAttachment = null;

      if (imageInput && type !== 'image') imageInput.value = '';
      if (videoInput && type !== 'video') videoInput.value = '';
      if (attachmentInput && type !== 'attachment') attachmentInput.value = '';

      if (type === 'image') selectedImage = file;
      if (type === 'video') selectedVideo = file;
      if (type === 'attachment') selectedAttachment = file;

      showFilePreview();
    });
  }

  function postMessage(content, image, video, audio, attachment, parentId) {
    if (!content && !image && !video && !audio && !attachment) return;

    const formData = new FormData();
    if (content) formData.append('content', content);
    if (image) formData.append('image', image);
    if (video) formData.append('video', video);
    if (audio) formData.append('audio', audio);
    if (attachment) formData.append('attachment', attachment);
    if (parentId) formData.append('parent_id', parentId);

    fetch(window.location.href, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrfToken()
      },
      body: formData
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.ok === false) {
          window.alert(data.error || 'Message could not be sent.');
          return;
        }

        // Show the sender's message immediately after the Django POST succeeds.
        // appendPost() has a duplicate guard, so the later WebSocket broadcast
        // will not create a second copy.
        if (data.message) {
          renderIncomingMessage(data.message);
        }
      })
      .catch((error) => console.error('Message save failed:', error));
  }

  function sendMainMessage(event) {
    event.preventDefault();
    const input = document.getElementById('chat-message-input');
    const content = input ? input.value.trim() : '';

    if (!content && !selectedImage && !selectedVideo && !selectedAttachment) return;

    postMessage(content, selectedImage, selectedVideo, null, selectedAttachment, null);
    sendTyping(false);
    window.clearTimeout(typingTimer);
    if (input) input.value = '';
    clearFiles();
  }

  function openEmojiPicker(card) {
    const picker = card.querySelector('[data-emoji-picker]');
    if (!picker) return;
    document.querySelectorAll('[data-emoji-picker]').forEach((item) => {
      if (item !== picker) item.classList.add('hidden');
    });
    picker.classList.toggle('hidden');
  }

  function updatePinnedBanner(pinnedMessage) {
    const banner = document.getElementById('pinned-banner');
    const text = document.getElementById('pinned-banner-text');
    const image = document.getElementById('pinned-banner-image');
    const unpinButton = document.getElementById('pinned-unpin');

    if (!banner || !text) return;

    if (!pinnedMessage) {
      banner.dataset.pinnedMessageId = '';
      banner.classList.add('hidden');
      if (image) {
        image.src = '';
        image.classList.add('hidden');
      }
      if (unpinButton) {
        unpinButton.dataset.pinUrl = '';
        unpinButton.classList.add('hidden');
      }
    } else {
      banner.dataset.pinnedMessageId = String(pinnedMessage.id);
      text.textContent = `${pinnedMessage.author}: ${pinnedMessage.content}`;
      banner.classList.remove('hidden');

      if (image) {
        if (pinnedMessage.image_url) {
          image.src = pinnedMessage.image_url;
          image.classList.remove('hidden');
        } else {
          image.src = '';
          image.classList.add('hidden');
        }
      }
      if (unpinButton && window.IS_ROOM_ADMIN) {
        unpinButton.dataset.pinUrl = pinnedMessage.pin_url || '';
        unpinButton.classList.remove('hidden');
      }
    }
  }

  function unpinPinnedMessage() {
    const unpinButton = document.getElementById('pinned-unpin');
    const url = unpinButton ? unpinButton.dataset.pinUrl : '';
    if (!url) return;

    fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest'
      }
    })
      .then((response) => response.json())
      .then((data) => {
        if (!data.ok) {
          window.alert(data.error || 'Could not unpin message.');
          return;
        }
        updatePinnedBanner(data.pinned_message);
        updatePinButtons(data.pinned_message);
      })
      .catch(() => window.alert('Could not unpin message.'));
  }

  function openPinnedMessage() {
    const banner = document.getElementById('pinned-banner');
    if (!banner) return;

    const messageId = banner.dataset.pinnedMessageId;
    if (!messageId) return;

    const messageCard = document.querySelector(
      `[data-message-id="${messageId}"]`
    );

    if (!messageCard) return;

    messageCard.scrollIntoView({
      behavior: 'smooth',
      block: 'center'
    });

    messageCard.classList.add('pinned-target-highlight');
    window.setTimeout(() => {
      messageCard.classList.remove('pinned-target-highlight');
    }, 1800);
  }

  function updatePinButtons(pinnedMessage) {
    const pinnedId = pinnedMessage ? String(pinnedMessage.id) : '';

    document.querySelectorAll('.post-card[data-message-id]').forEach((card) => {
      const isPinned = String(card.dataset.messageId) === pinnedId;
      card.dataset.isPinned = isPinned ? 'true' : 'false';

      const button = card.querySelector('[data-pin-message]');
      const icon = card.querySelector('[data-pin-icon]');
      if (!button || !icon) return;

      button.classList.toggle('pin-active', isPinned);
      button.setAttribute('aria-label', isPinned ? 'Unpin message' : 'Pin message');
      button.setAttribute('title', isPinned ? 'Unpin message' : 'Pin message');
      icon.classList.toggle('ph-push-pin-fill', isPinned);
      icon.classList.toggle('ph-push-pin', !isPinned);
    });
  }

  function togglePinnedMessage(card) {
    const url = card.dataset.pinUrl;
    if (!url) return;

    fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest'
      }
    })
      .then((response) => response.json())
      .then((data) => {
        if (!data.ok) {
          window.alert(data.error || 'Could not update pinned message.');
          return;
        }

        updatePinnedBanner(data.pinned_message);
        updatePinButtons(data.pinned_message);
      })
      .catch(() => window.alert('Could not update pinned message.'));
  }

  function sendReaction(card, emoji) {
    const url = card.dataset.likeUrl;
    if (!url) return;

    const data = new URLSearchParams();
    data.append('emoji', emoji);

    fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: data.toString()
    })
      .then((response) => response.json())
      .then((result) => {
        const icon = card.querySelector('[data-react-icon]');
        const count = card.querySelector('[data-react-count]');
        if (icon) icon.textContent = result.user_emoji || '';
        if (count) count.textContent = result.count;
      });
  }

  function updateTypingIndicator() {
    const indicator = document.getElementById('typing-indicator');
    const text = document.getElementById('typing-text');
    if (!indicator || !text) return;

    const names = Array.from(typingUsers.values());

    if (!names.length) {
      indicator.classList.add('hidden');
      indicator.classList.remove('flex');
      return;
    }

    if (names.length === 1) {
      text.textContent = `${names[0]} is typing...`;
    } else if (names.length === 2) {
      text.textContent = `${names[0]} and ${names[1]} are typing...`;
    } else {
      text.textContent = `${names.length} people are typing...`;
    }

    indicator.classList.remove('hidden');
    indicator.classList.add('flex');
  }

  function sendTyping(isTyping) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: 'typing', is_typing: isTyping }));
  }

  function connectWebSocket() {
    const roomElement = document.getElementById('room-name');
    const roomName = roomElement ? JSON.parse(roomElement.textContent) : '';
    if (!roomName) return;

    const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    socket = new WebSocket(`${protocol}${window.location.host}/ws/chat/${encodeURIComponent(roomName)}/`);
    window._ws = socket;

    socket.onopen = () => console.log('Chat WebSocket connected');

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'online') {
        const count = document.getElementById('online-count');
        if (count) count.textContent = data.count;
        return;
      }

      if (data.type === 'message') {
        renderIncomingMessage(data.message);
        return;
      }

      if (data.type === 'reaction') {
        const card = document.querySelector(`[data-message-id="${data.message_id}"]`);
        if (card) {
          const count = card.querySelector('[data-react-count]');
          if (count) count.textContent = data.count;
        }
        return;
      }

      if (data.type === 'message_edit') {
        const card = document.querySelector(`[data-message-id="${data.message_id}"]`);
        if (card) {
          const content = card.querySelector('[data-message-content]');
          if (content) {
            if (data.deleted_for_everyone) {
              content.textContent = '🚫 This message was deleted';
              content.classList.add('deleted-copy');
              card.querySelectorAll('.media-frame, .attachment-row, [data-poll-id]').forEach((item) => item.remove());
            } else {
              content.innerHTML = linkifyText(data.content);
              content.classList.remove('deleted-copy');
            }
          }
        }
        return;
      }

      if (data.type === 'message_delete') {
        const card = document.querySelector(`[data-message-id="${data.message_id}"]`);
        if (card) card.remove();
        return;
      }

      if (data.type === 'poll_update') {
        if (window.updateRealtimePoll) window.updateRealtimePoll(data.poll);
        return;
      }

      if (data.type === 'pinned_update') {
        updatePinnedBanner(data.pinned_message);
        updatePinButtons(data.pinned_message);
        return;
      }

      if (data.type === 'typing' && data.username !== CURRENT_USERNAME) {
        if (data.is_typing) {
          typingUsers.set(data.username, data.full_name || data.username);
        } else {
          typingUsers.delete(data.username);
        }
        updateTypingIndicator();
      }
    };

    socket.onclose = () => {
      window.setTimeout(connectWebSocket, 3000);
    };
  }

  document.addEventListener('DOMContentLoaded', () => {
    installRealtimeLinkStyle();
    openAtLatestMessage();

    const form = document.getElementById('chat-message-form');
    const mainInput = document.getElementById('chat-message-input');
    const pinnedBanner = document.getElementById('pinned-banner');
    const pinnedUnpin = document.getElementById('pinned-unpin');

    if (pinnedBanner) {
      pinnedBanner.addEventListener('click', openPinnedMessage);
    }

    if (pinnedUnpin) {
      pinnedUnpin.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        unpinPinnedMessage();
      });
    }

    const imageLightbox = document.getElementById('image-lightbox');
    const imageLightboxClose = document.getElementById('image-lightbox-close');

    if (imageLightboxClose) {
      imageLightboxClose.addEventListener('click', closeImageLightbox);
    }

    if (imageLightbox) {
      imageLightbox.addEventListener('click', (event) => {
        if (event.target === imageLightbox) closeImageLightbox();
      });
    }

    const imageLightboxImage = document.getElementById('image-lightbox-image');

    if (imageLightboxImage) {
      imageLightboxImage.addEventListener('touchstart', (event) => {
        if (event.touches.length === 2) {
          pinchStartDistance = touchDistance(event.touches[0], event.touches[1]);
          pinchStartScale = lightboxScale;
          return;
        }

        if (event.touches.length === 1) {
          const now = Date.now();
          const isDoubleTap = now - lastImageTap < 280;
          lastImageTap = now;

          if (isDoubleTap) {
            lightboxScale = lightboxScale > 1 ? 1 : 2.5;
            if (lightboxScale === 1) {
              lightboxOffsetX = 0;
              lightboxOffsetY = 0;
            }
            applyLightboxTransform();
            event.preventDefault();
            return;
          }

          panStartX = event.touches[0].clientX;
          panStartY = event.touches[0].clientY;
          panOriginX = lightboxOffsetX;
          panOriginY = lightboxOffsetY;
        }
      }, { passive: false });

      imageLightboxImage.addEventListener('touchmove', (event) => {
        if (event.touches.length === 2 && pinchStartDistance) {
          const distance = touchDistance(event.touches[0], event.touches[1]);
          lightboxScale = Math.min(4, Math.max(1, pinchStartScale * (distance / pinchStartDistance)));

          if (lightboxScale === 1) {
            lightboxOffsetX = 0;
            lightboxOffsetY = 0;
          }

          applyLightboxTransform();
          event.preventDefault();
          return;
        }

        if (event.touches.length === 1 && lightboxScale > 1) {
          lightboxOffsetX = panOriginX + (event.touches[0].clientX - panStartX);
          lightboxOffsetY = panOriginY + (event.touches[0].clientY - panStartY);
          applyLightboxTransform();
          event.preventDefault();
        }
      }, { passive: false });

      imageLightboxImage.addEventListener('touchend', () => {
        pinchStartDistance = 0;
      });
    }

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeImageLightbox();
    });

    const voiceRecordButton = document.getElementById('voice-record-button');
    const voiceCancelButton = document.getElementById('voice-cancel-button');
    let voiceRecorder = null;
    let voiceStream = null;
    let voiceChunks = [];
    let discardVoiceRecording = false;

    async function toggleVoiceRecording() {
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        window.alert('Voice recording is not supported in this browser.');
        return;
      }

      if (voiceRecorder && voiceRecorder.state === 'recording') {
        voiceRecorder.stop();
        return;
      }

      try {
        voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        voiceChunks = [];
        discardVoiceRecording = false;

        try {
          voiceRecorder = new MediaRecorder(voiceStream, { mimeType: 'audio/webm' });
        } catch (error) {
          voiceRecorder = new MediaRecorder(voiceStream);
        }

        voiceRecorder.ondataavailable = (event) => {
          if (event.data.size > 0) voiceChunks.push(event.data);
        };

        voiceRecorder.onstop = () => {
          if (!discardVoiceRecording && voiceChunks.length) {
            const voiceBlob = new Blob(voiceChunks, { type: 'audio/webm' });
            const voiceFile = new File([voiceBlob], `voice-${Date.now()}.webm`, { type: 'audio/webm' });
            postMessage('', null, null, voiceFile, null, null);
          }

          if (voiceStream) voiceStream.getTracks().forEach((track) => track.stop());
          voiceRecorder = null;
          voiceStream = null;
          voiceChunks = [];
          discardVoiceRecording = false;

          if (voiceRecordButton) {
            voiceRecordButton.classList.remove('voice-recording');
            voiceRecordButton.innerHTML = '<i class="ph ph-microphone"></i>';
            voiceRecordButton.setAttribute('aria-label', 'Record voice message');
          }

          if (voiceCancelButton) voiceCancelButton.classList.add('hidden');
        };

        voiceRecorder.start();
        if (voiceRecordButton) {
          voiceRecordButton.classList.add('voice-recording');
          voiceRecordButton.innerHTML = '<i class="ph ph-stop-circle"></i>';
          voiceRecordButton.setAttribute('aria-label', 'Stop and send voice message');
        }

        if (voiceCancelButton) voiceCancelButton.classList.remove('hidden');
      } catch (error) {
        window.alert('Microphone permission is required to send a voice message.');
      }
    }

    if (voiceRecordButton) {
      voiceRecordButton.addEventListener('click', toggleVoiceRecording);
    }

    if (voiceCancelButton) {
      voiceCancelButton.addEventListener('click', () => {
        if (voiceRecorder && voiceRecorder.state === 'recording') {
          discardVoiceRecording = true;
          voiceRecorder.stop();
        }
      });
    }

    if (form) form.addEventListener('submit', sendMainMessage);

    if (mainInput) {
      mainInput.addEventListener('input', () => {
        const hasText = mainInput.value.trim().length > 0;

        if (!hasText) {
          window.clearTimeout(typingTimer);
          sendTyping(false);
          return;
        }

        sendTyping(true);
        window.clearTimeout(typingTimer);
        typingTimer = window.setTimeout(() => sendTyping(false), 1200);
      });

      mainInput.addEventListener('blur', () => {
        window.clearTimeout(typingTimer);
        sendTyping(false);
      });
    }

    bindFileInput('#media-input', 'image');
    bindFileInput('#video-input', 'video');
    bindFileInput('#file-input', 'attachment');

    const clear = document.getElementById('file-preview-clear');
    if (clear) clear.addEventListener('click', (event) => {
      event.preventDefault();
      clearFiles();
    });

    document.addEventListener('click', (event) => {
      const target = event.target;
      const card = target.closest('.post-card');

      const zoomImage = target.closest('[data-zoom-image]');
      if (zoomImage) {
        event.preventDefault();
        openImageLightbox(zoomImage.currentSrc || zoomImage.src, zoomImage.alt);
        return;
      }

      const reaction = target.closest('[data-react-btn]');
      if (reaction && card) {
        event.preventDefault();
        openEmojiPicker(card);
        return;
      }

      const emoji = target.closest('[data-emoji]');
      if (emoji && card) {
        event.preventDefault();
        sendReaction(card, emoji.dataset.emoji);
        const picker = emoji.closest('[data-emoji-picker]');
        if (picker) picker.classList.add('hidden');
        return;
      }

      const replyButton = target.closest('[data-reply-btn]');
      if (replyButton && card) {
        event.preventDefault();
        const replyForm = card.querySelector('[data-reply-inline]');
        if (replyForm) {
          replyForm.classList.toggle('hidden');
          if (!replyForm.classList.contains('hidden')) {
            replyForm.querySelector('input[name="content"]').focus();
          }
        }
        return;
      }

      const pinButton = target.closest('[data-pin-message]');
      if (pinButton && card) {
        event.preventDefault();
        togglePinnedMessage(card);
        return;
      }

      const editButton = target.closest('[data-edit-message]');
      if (editButton && card) {
        event.preventDefault();
        const contentElement = card.querySelector('[data-message-content]');
        const oldContent = contentElement ? contentElement.textContent.trim() : '';
        const newContent = window.prompt('Edit your message:', oldContent);
        const editUrl = editButton.dataset.editUrl;

        if (newContent === null || !newContent.trim() || !editUrl) return;

        const formData = new URLSearchParams();
        formData.append('content', newContent.trim());

        fetch(editUrl, {
          method: 'POST',
          headers: {
            'X-CSRFToken': csrfToken(),
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded'
          },
          body: formData.toString()
        }).then((response) => response.json()).then((data) => {
          if (data.ok && contentElement) contentElement.innerHTML = linkifyText(data.content);
        });
        return;
      }

      const deleteButton = target.closest('[data-delete-message]');
      if (deleteButton && card) {
        event.preventDefault();
        const deleteUrl = deleteButton.dataset.deleteUrl;

        if (!deleteUrl || !window.confirm('Delete this message?')) return;

        fetch(deleteUrl, {
          method: 'POST',
          headers: {
            'X-CSRFToken': csrfToken(),
            'X-Requested-With': 'XMLHttpRequest'
          }
        }).then((response) => response.json()).then((data) => {
          if (!data.ok) {
            window.alert(data.error || 'Could not delete this message.');
            return;
          }

          if (data.deleted_for_everyone) {
            const messageText = card.querySelector('[data-message-content]');
            if (messageText) {
              messageText.textContent = '🚫 This message was deleted';
              messageText.classList.add('deleted-copy');
            }
            card.querySelectorAll('.media-frame, .attachment-row, [data-poll-id]').forEach((item) => item.remove());
          } else {
            card.remove();
          }
        });
        return;
      }

      const viewReplies = target.closest('[data-view-replies]');
      if (viewReplies && card) {
        event.__repliesHandled = true;
        event.preventDefault();

        const repliesList = card.querySelector('[data-replies-list]');
        const items = repliesList.querySelectorAll('[data-reply-item]');
        const label = viewReplies.querySelector('[data-reply-count-text]');
        const caret = viewReplies.querySelector('[data-reply-caret]');
        const isExpanded = repliesList.dataset.expanded === 'true';

        repliesList.dataset.expanded = isExpanded ? 'false' : 'true';

        items.forEach((item, index) => {
          item.classList.toggle('hidden', isExpanded && index < items.length - 1);
        });

        if (label) {
          label.textContent = isExpanded
            ? `View all ${items.length} replies`
            : 'Collapse replies';
        }

        if (caret) {
          caret.classList.toggle('ph-caret-down', isExpanded);
          caret.classList.toggle('ph-caret-up', !isExpanded);
        }

        return;
      }

      const cancelReply = target.closest('[data-cancel-reply]');
      if (cancelReply) {
        event.preventDefault();
        const replyForm = cancelReply.closest('[data-reply-inline]');
        if (replyForm) {
          replyForm.classList.add('hidden');
          replyForm.reset();
        }
      }
    });

    document.addEventListener('submit', (event) => {
      const replyForm = event.target.closest('[data-reply-inline]');
      if (!replyForm) return;

      event.preventDefault();
      const card = replyForm.closest('.post-card');
      const input = replyForm.querySelector('input[name="content"]');
      const imageInput = replyForm.querySelector('[data-reply-image]');
      const videoInput = replyForm.querySelector('[data-reply-video]');
      const attachmentInput = replyForm.querySelector('[data-reply-attachment]');
      const content = input ? input.value.trim() : '';
      const image = imageInput ? imageInput.files[0] : null;
      const video = videoInput ? videoInput.files[0] : null;
      const attachment = attachmentInput ? attachmentInput.files[0] : null;

      if (!card || (!content && !image && !video && !attachment)) return;

      postMessage(content, image, video, null, attachment, card.dataset.messageId);
      replyForm.reset();
      replyForm.classList.add('hidden');
    });

    connectWebSocket();
  });


  // ── WhatsApp-style messaging controls ──
  document.addEventListener('DOMContentLoaded', () => {
    const optionsOpen = document.getElementById('wa-chat-options-open');
    const optionsModal = document.getElementById('wa-chat-options-modal');
    const forwardModal = document.getElementById('wa-forward-modal');
    const forwardRoom = document.getElementById('wa-forward-room');
    const forwardSend = document.getElementById('wa-forward-send');
    const disappearing = document.getElementById('wa-disappearing');
    const disappearingSave = document.getElementById('wa-disappearing-save');
    let selectedForwardUrl = '';

    const postData = (url, data = {}) => {
      const body = new URLSearchParams(data);
      return fetch(url, {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrfToken(),
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        body: body.toString()
      }).then((response) => response.json());
    };

    const closeAllSheets = () => {
      document.querySelectorAll('.wa-chat-control').forEach((modal) => modal.classList.remove('open'));
    };

    if (optionsOpen) optionsOpen.addEventListener('click', () => optionsModal.classList.add('open'));
    document.querySelectorAll('[data-wa-close]').forEach((button) => button.addEventListener('click', closeAllSheets));
    document.querySelectorAll('.wa-chat-control').forEach((modal) => {
      modal.addEventListener('click', (event) => { if (event.target === modal) modal.classList.remove('open'); });
    });

    if (disappearing && window.WA_DISAPPEARING_VALUE) {
      disappearing.value = window.WA_DISAPPEARING_VALUE;
    }

    document.addEventListener('click', (event) => {
      const forward = event.target.closest('[data-forward-message]');
      if (forward) {
        selectedForwardUrl = forward.dataset.forwardUrl || '';
        forwardModal.classList.add('open');
        return;
      }

      const star = event.target.closest('[data-star-message]');
      if (star) {
        postData(star.dataset.starUrl).then((data) => {
          if (data.ok) star.classList.toggle('wa-starred', data.starred);
        });
      }
    });

    if (forwardSend) forwardSend.addEventListener('click', () => {
      if (!selectedForwardUrl || !forwardRoom.value) return;
      postData(selectedForwardUrl, { room_name: forwardRoom.value }).then((data) => {
        if (data.ok) {
          closeAllSheets();
          forwardRoom.value = '';
        } else {
          window.alert(data.error || 'Could not forward message.');
        }
      });
    });

    document.querySelectorAll('[data-wa-mute]').forEach((button) => {
      button.addEventListener('click', () => {
        postData(window.WA_MUTE_URL, { minutes: button.dataset.waMute }).then(() => closeAllSheets());
      });
    });

    const archive = document.getElementById('wa-archive-toggle');
    if (archive) archive.addEventListener('click', () => {
      postData(window.WA_ARCHIVE_URL).then((data) => {
        if (data.ok) archive.textContent = data.is_archived ? 'Unarchive chat' : 'Archive chat';
      });
    });

    if (disappearingSave) disappearingSave.addEventListener('click', () => {
      postData(window.WA_DISAPPEARING_URL, { seconds: disappearing.value }).then((data) => {
        if (data.ok) closeAllSheets();
        else window.alert(data.error || 'Could not save timer.');
      });
    });

    if (window.WA_READ_URL) postData(window.WA_READ_URL).catch(() => {});
  });



  // WhatsApp-style action row: tap the message itself to reveal controls.
  document.addEventListener('click', (event) => {
    const card = event.target.closest('.post-card');

    if (!card) {
      document.querySelectorAll('.post-card.action-open').forEach((item) => {
        item.classList.remove('action-open');
      });
      return;
    }

    // Buttons, links, media and form controls must keep their own behavior.
    if (event.target.closest('button, a, input, textarea, select, label, video, audio, [data-zoom-image]')) {
      return;
    }

    document.querySelectorAll('.post-card.action-open').forEach((item) => {
      if (item !== card) item.classList.remove('action-open');
    });
    card.classList.toggle('action-open');
  });

})();
