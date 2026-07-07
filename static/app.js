// ==================== Theme Toggle ====================

(function() {
    const html = document.documentElement;
    const toggle = document.getElementById('theme-toggle');

    // Загружаем сохранённую тему
    const saved = localStorage.getItem('theme');
    if (saved) {
        html.setAttribute('data-theme', saved);
    } else {
        html.setAttribute('data-theme', 'auto');
    }

    if (toggle) {
        toggle.addEventListener('click', function() {
            const current = html.getAttribute('data-theme');
            let next;
            if (current === 'auto') {
                next = 'dark';
            } else if (current === 'dark') {
                next = 'light';
            } else {
                next = 'auto';
            }
            html.setAttribute('data-theme', next);
            localStorage.setItem('theme', next);
        });
    }
})();

// ==================== Lightbox ====================

window.openLightbox = function(src) {
    const lb = document.getElementById('lightbox');
    const content = document.getElementById('lightbox-content');
    content.innerHTML = '<img src="' + src + '" alt="full size">';
    lb.style.display = 'flex';
    document.body.style.overflow = 'hidden';
};

window.closeLightbox = function() {
    const lb = document.getElementById('lightbox');
    lb.style.display = 'none';
    document.body.style.overflow = '';
};

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeLightbox();
    }
});

// ==================== Delete Button ====================

window.updateDeleteButton = function() {
    const checked = document.querySelectorAll('.card-checkbox:checked');
    const btn = document.getElementById('delete-selected');
    if (btn) {
        btn.disabled = checked.length === 0;
        btn.textContent = checked.length ? '\uD83D\uDDD1 Удалить (' + checked.length + ')' : '\uD83D\uDDD1 Удалить выбранные';
    }
};

(function() {
    const btn = document.getElementById('delete-selected');
    if (btn) {
        btn.addEventListener('click', async function() {
            const checked = document.querySelectorAll('.card-checkbox:checked');
            if (checked.length === 0) return;
            if (!confirm('Удалить ' + checked.length + ' сообщений? Это нельзя отменить.')) return;

            const ids = Array.from(checked).map(function(c) { return c.value; }).join(',');
            const formData = new FormData();
            formData.append('message_ids', ids);

            try {
                const resp = await fetch('/delete', { method: 'POST', body: formData });
                if (resp.ok) {
                    window.location.reload();
                } else {
                    alert('Ошибка при удалении');
                }
            } catch (e) {
                alert('Ошибка: ' + e.message);
            }
        });
    }
})();

// ==================== AJAX Pagination ====================

(function() {
    // Подгружаем следующую страницу при скролле (опционально)
    let loading = false;

    window.addEventListener('scroll', function() {
        if (loading) return;
        const scrollTop = window.scrollY || document.documentElement.scrollTop;
        const windowHeight = window.innerHeight;
        const docHeight = document.documentElement.scrollHeight;

        if (scrollTop + windowHeight >= docHeight - 500) {
            const nextLink = document.querySelector('.pagination a[href*="direction=older"]');
            if (!nextLink) return;
            loadMore(nextLink.href);
        }
    });

    async function loadMore(url) {
        loading = true;
        try {
            const apiUrl = url.replace('/?', '/api/messages?');
            const resp = await fetch(apiUrl);
            const data = await resp.json();

            if (data.messages && data.messages.length > 0) {
                const grid = document.getElementById('cards-grid');
                if (!grid) return;

                // Удаляем empty state если есть
                const empty = grid.querySelector('.empty-state');
                if (empty) empty.remove();

                data.messages.forEach(function(msg) {
                    const card = createCard(msg);
                    grid.appendChild(card);
                });

                // Обновляем пагинацию
                updatePagination(data);
            }
        } catch (e) {
            console.error('Load more error:', e);
        } finally {
            loading = false;
        }
    }

    function createCard(msg) {
        const div = document.createElement('div');
        div.className = 'card';
        div.setAttribute('data-message-id', msg.message_id);

        const iconMap = {
            'photo': '\uD83D\uDDBC\uFE0F', 'video': '\uD83C\uDFAC', 'voice': '\uD83C\uDFA4',
            'audio': '\uD83C\uDFB5', 'document': '\uD83D\uDCC4', 'sticker': '\uD83D\uDE1C',
            'gif': '\u2728', 'round_video': '\uD83D\uDD35', 'custom_emoji': '\uD83D\uDE00',
            'text': '\uD83D\uDCDD', 'album': '\uD83D\uDCDA'
        };
        const icon = iconMap[msg.type_label] || '\uD83D\uDCE6';

        let thumbHtml = '';
        if (msg.thumbnail_path) {
            thumbHtml = '<a href="/message/' + msg.message_id + '" class="card-thumb-link">' +
                '<img src="/thumbnail/' + msg.message_id + '" class="card-thumb" loading="lazy">' +
                '</a>';
        } else if (msg.file_path && ['photo', 'video', 'gif', 'round_video'].includes(msg.type_label)) {
            thumbHtml = '<a href="/message/' + msg.message_id + '" class="card-thumb-link">' +
                '<img src="/media/' + msg.file_path + '" class="card-thumb" loading="lazy">' +
                '</a>';
        } else if (msg.file_path) {
            thumbHtml = '<a href="/message/' + msg.message_id + '" class="card-thumb-link">' +
                '<div class="card-icon-placeholder">' + icon + '</div></a>';
        } else {
            thumbHtml = '<a href="/message/' + msg.message_id + '" class="card-thumb-link">' +
                '<div class="card-text-preview">' + (msg.text || '\u0411\u0435\u0437 \u0442\u0435\u043A\u0441\u0442\u0430').substring(0, 200) + '</div></a>';
        }

        div.innerHTML =
            '<input type="checkbox" class="card-checkbox" value="' + msg.message_id + '" onclick="event.stopPropagation(); updateDeleteButton()">' +
            '<span class="card-type-badge">' + icon + '</span>' +
            thumbHtml +
            '<div class="card-body">' +
                '<div class="card-text">' + (msg.text ? msg.text.substring(0, 150) : '<span class="no-text">\u2014</span>') + '</div>' +
                '<div class="card-meta">' +
                    '<span class="card-date">' + (msg.date_formatted || '') + '</span>' +
                    (msg.file_size ? '<span class="card-size">' + (msg.size_formatted || '') + '</span>' : '') +
                    (msg.duration ? '<span class="card-duration">' + (msg.duration_formatted || '') + '</span>' : '') +
                '</div>' +
                (msg.grouped_id ? '<a href="/album/' + msg.grouped_id + '" class="card-album-link">\uD83D\uDCDA Альбом</a>' : '') +
            '</div>';

        return div;
    }

    function updatePagination(data) {
        if (data.has_next && data.next_cursor) {
            // Обновляем ссылку "Вперёд"
            const prevNext = document.querySelector('.pagination a[href*="direction=older"]');
            if (prevNext) {
                // Парсим текущий URL и заменяем cursor
                const url = new URL(prevNext.href);
                url.searchParams.set('cursor', data.next_cursor);
                prevNext.href = url.toString();
            }
        } else {
            // Убираем ссылку "Вперёд"
            const nextLink = document.querySelector('.pagination a[href*="direction=older"]');
            if (nextLink) {
                const span = document.createElement('span');
                span.className = 'btn btn-page disabled';
                span.textContent = '\u0412\u043F\u0435\u0440\u0451\u0434 \u2192';
                nextLink.parentNode.replaceChild(span, nextLink);
            }
        }
    }
})();