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
    if (!lb || !content) return;
    content.innerHTML = '<img src="' + src + '" alt="full size">';
    lb.style.display = 'flex';
    document.body.style.overflow = 'hidden';
};

window.closeLightbox = function() {
    const lb = document.getElementById('lightbox');
    if (!lb) return;
    lb.style.display = 'none';
    document.body.style.overflow = '';
};

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeLightbox();
    }
});

