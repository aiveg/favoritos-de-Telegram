// ==================== Theme Toggle ====================

(function() {
    const html = document.documentElement;
    const toggle = document.getElementById('theme-toggle');

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

// ==================== Lightbox with Gallery Navigation ====================

window._galleryItems = [];
window._galleryIndex = 0;

window.openLightbox = function(src) {
    window._galleryItems = [src];
    window._galleryIndex = 0;
    showImage(src);
};

window.openGallery = function(items, startIndex) {
    window._galleryItems = items;
    window._galleryIndex = startIndex || 0;
    showImage(items[window._galleryIndex]);
};

function showImage(src) {
    const lb = document.getElementById('lightbox');
    const content = document.getElementById('lightbox-content');
    if (!lb || !content) return;
    const isVideo = src.match(/\.(mp4|mov|webm)(\?|$)/i);
    if (isVideo) {
        content.innerHTML = '<video controls autoplay style="max-width:95vw;max-height:95vh"><source src="' + src + '" type="video/mp4"></video>';
    } else {
        content.innerHTML = '<img src="' + src + '" alt="full size">';
    }
    updateGalleryNav();
    lb.style.display = 'flex';
    document.body.style.overflow = 'hidden';
}

function updateGalleryNav() {
    const lb = document.getElementById('lightbox');
    if (!lb) return;
    const totalItems = window._galleryItems.length;
    // Удаляем старые стрелки если есть
    var prevBtn = document.getElementById('lb-prev');
    var nextBtn = document.getElementById('lb-next');
    if (prevBtn) prevBtn.remove();
    if (nextBtn) nextBtn.remove();
    if (totalItems <= 1) return;

    prevBtn = document.createElement('button');
    prevBtn.id = 'lb-prev';
    prevBtn.className = 'lb-arrow lb-arrow-left';
    prevBtn.innerHTML = '&#10094;';
    prevBtn.onclick = function(e) { e.stopPropagation(); galleryPrev(); };

    nextBtn = document.createElement('button');
    nextBtn.id = 'lb-next';
    nextBtn.className = 'lb-arrow lb-arrow-right';
    nextBtn.innerHTML = '&#10095;';
    nextBtn.onclick = function(e) { e.stopPropagation(); galleryNext(); };

    lb.appendChild(prevBtn);
    lb.appendChild(nextBtn);
}

function galleryPrev() {
    if (window._galleryItems.length <= 1) return;
    window._galleryIndex = (window._galleryIndex - 1 + window._galleryItems.length) % window._galleryItems.length;
    showImage(window._galleryItems[window._galleryIndex]);
}

function galleryNext() {
    if (window._galleryItems.length <= 1) return;
    window._galleryIndex = (window._galleryIndex + 1) % window._galleryItems.length;
    showImage(window._galleryItems[window._galleryIndex]);
}

window.closeLightbox = function() {
    const lb = document.getElementById('lightbox');
    if (!lb) return;
    lb.style.display = 'none';
    document.body.style.overflow = '';
    // Убираем стрелки
    var prevBtn = document.getElementById('lb-prev');
    var nextBtn = document.getElementById('lb-next');
    if (prevBtn) prevBtn.remove();
    if (nextBtn) nextBtn.remove();
    window._galleryItems = [];
};

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeLightbox();
    } else if (e.key === 'ArrowLeft') {
        galleryPrev();
    } else if (e.key === 'ArrowRight') {
        galleryNext();
    }
});