/**
 * R2C.ai — Research Trends → Ad & Picture Generator
 * Client-side Controller & UI State Manager
 */

let state = {
    topics: [],
    selectedTopicId: null,
    activeView: 'canvas', // 'canvas' | 'image'
    currentTheme: 'Cyber Dark (Cyan)',
    currentFormat: 'X (Twitter)',
    isScraping: false,
    isGenerating: false
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    loadInitialData();
});

/**
 * Load initial sample topics from backend
 */
async function loadInitialData() {
    try {
        const response = await fetch('/api/sample-data');
        const data = await response.json();
        if (data && data.topics && data.topics.length > 0) {
            state.topics = data.topics;
            renderTopicCards();
            selectTopic(data.topics[0].id);
        }
    } catch (err) {
        console.error('Failed to load initial data:', err);
    }
}

/**
 * Render Topic Cards Grid (Step 1)
 */
function renderTopicCards() {
    const container = document.getElementById('topics-grid-container');
    const badge = document.getElementById('topics-count-badge');
    if (!container) return;

    badge.textContent = `${state.topics.length} Topics Extracted`;
    container.innerHTML = '';

    state.topics.forEach((topic) => {
        const isSelected = topic.id === state.selectedTopicId;
        const card = document.createElement('div');
        card.className = `topic-card ${isSelected ? 'selected' : ''}`;
        card.id = `topic-card-${topic.id}`;
        card.onclick = () => selectTopic(topic.id);

        card.innerHTML = `
            <div class="topic-card-top-row">
                <span class="category-tag">${escapeHtml(topic.category || 'AI RESEARCH')}</span>
                <span class="stat-callout-pill">${escapeHtml(topic.stat_callout || '')}</span>
            </div>
            <h3 class="topic-card-title">${escapeHtml(topic.title)}</h3>
            <p class="topic-card-summary">${escapeHtml(topic.summary)}</p>
            <div class="why-matters-box">
                <span class="why-matters-label">Why it matters for R2C: </span>
                <span class="why-matters-text">${escapeHtml(topic.why_it_matters || '')}</span>
            </div>
        `;
        container.appendChild(card);
    });
}

/**
 * Select active topic and update Steps 2 & 3
 */
function selectTopic(topicId) {
    state.selectedTopicId = topicId;
    const topic = state.topics.find(t => t.id === topicId);
    if (!topic) return;

    // Update active class on cards
    document.querySelectorAll('.topic-card').forEach(card => {
        card.classList.remove('selected');
    });
    const activeCard = document.getElementById(`topic-card-${topicId}`);
    if (activeCard) activeCard.classList.add('selected');

    // Update Step 2 (Prompt)
    const promptTextarea = document.getElementById('prompt-textarea');
    const targetTopicDisplay = document.getElementById('target-topic-display');
    if (promptTextarea) promptTextarea.value = topic.image_prompt || topic.prompt || '';
    if (targetTopicDisplay) targetTopicDisplay.textContent = topic.title;

    // Update Step 3 (Visual Canvas)
    updateCanvasView(topic);

    // Update Step 3.5 (Ad Copy Card)
    updateCopyCard(topic);
}

/**
 * Update Graphic Canvas elements
 */
function updateCanvasView(topic) {
    const statEl = document.getElementById('ad-canvas-stat');
    const headlineEl = document.getElementById('ad-canvas-headline');
    const subheadlineEl = document.getElementById('ad-canvas-subheadline');
    const ctaEl = document.getElementById('ad-canvas-cta');
    const imgEl = document.getElementById('ad-generated-img');
    const imagePlaceholder = document.getElementById('ad-image-placeholder');

    if (statEl) statEl.textContent = topic.stat_badge || '3.8X ACCURACY BOOST';
    if (headlineEl) headlineEl.textContent = topic.headline || topic.title;
    if (subheadlineEl) subheadlineEl.textContent = topic.subheadline || topic.summary;
    if (ctaEl) ctaEl.textContent = `${topic.cta || 'Deploy Workflows'} →`;

    if (imgEl) {
        imgEl.hidden = !topic.image_url;
        if (topic.image_url) {
            imgEl.src = topic.image_url;
        }
    }
    if (imagePlaceholder) imagePlaceholder.hidden = Boolean(topic.image_url);
}

/**
 * Update Ad Copy Card elements
 */
function updateCopyCard(topic) {
    const audienceEl = document.getElementById('copy-target-audience');
    const headlineEl = document.getElementById('copy-headline');
    const bodyEl = document.getElementById('copy-body');
    const hashtagsEl = document.getElementById('copy-hashtags');
    const ctaTextEl = document.getElementById('copy-cta-text');
    const statTextEl = document.getElementById('copy-stat-text');

    if (audienceEl) audienceEl.textContent = `Target Audience: ${topic.target_persona || 'Enterprise Automation Leaders'}`;
    if (headlineEl) headlineEl.textContent = topic.headline || topic.title;
    if (bodyEl) bodyEl.textContent = topic.body_copy || topic.summary;
    
    if (hashtagsEl) {
        if (Array.isArray(topic.hashtags)) {
            hashtagsEl.textContent = topic.hashtags.join(' ');
        } else {
            hashtagsEl.textContent = topic.hashtags || '#AIInnovation #ResearchCommercialization';
        }
    }

    if (ctaTextEl) ctaTextEl.textContent = topic.cta || 'Deploy Agentic Workflows';
    if (statTextEl) statTextEl.textContent = topic.stat_badge || '3.8X ACCURACY BOOST';
}

/**
 * Switch between Canvas and AI Image view
 */
function switchAdView(viewMode) {
    state.activeView = viewMode;
    const canvasTab = document.getElementById('tab-canvas-view');
    const imageTab = document.getElementById('tab-image-view');
    const canvasContainer = document.getElementById('ad-graphic-canvas');
    const imageContainer = document.getElementById('ad-image-container');

    if (viewMode === 'canvas') {
        canvasTab.classList.add('active');
        imageTab.classList.remove('active');
        canvasContainer.classList.remove('hidden');
        imageContainer.classList.add('hidden');
    } else {
        imageTab.classList.add('active');
        canvasTab.classList.remove('active');
        imageContainer.classList.remove('hidden');
        canvasContainer.classList.add('hidden');
    }
}

/**
 * Handle URL submission and pipeline execution
 */
async function submitUrls() {
    const textarea = document.getElementById('url-input-textarea');
    const btn = document.getElementById('btn-scrape-submit');
    const spinner = document.getElementById('scrape-spinner');
    const loadingState = document.getElementById('pipeline-loading-state');
    const urlsText = textarea.value.trim();

    if (!urlsText) {
        showToast('Please enter at least one URL.', 'error');
        return;
    }

    state.isScraping = true;
    btn.disabled = true;
    spinner.classList.remove('hidden');
    loadingState.classList.remove('hidden');

    try {
        const response = await fetch('/api/scrape-and-extract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls: urlsText })
        });
        const data = await response.json();

        if (data && data.topics && data.topics.length > 0) {
            state.topics = data.topics;
            renderTopicCards();
            selectTopic(data.topics[0].id);
            showToast('Links scraped and topics extracted successfully!');
            // Scroll smoothly to topics section
            document.getElementById('section-topics').scrollIntoView({ behavior: 'smooth' });
        } else {
            showToast('No topics extracted. Please verify the URLs.', 'error');
        }
    } catch (err) {
        console.error('Scrape error:', err);
        showToast('Scraping completed with fallback data.', 'info');
    } finally {
        state.isScraping = false;
        btn.disabled = false;
        spinner.classList.add('hidden');
        loadingState.classList.add('hidden');
    }
}

/**
 * Handle Generate Ad button click
 */
async function generateAdFromPrompt() {
    const btn = document.getElementById('btn-generate-ad');
    const spinner = document.getElementById('generate-spinner');

    state.isGenerating = true;
    btn.disabled = true;
    spinner.classList.remove('hidden');

    try {
        const prompt = document.getElementById('prompt-textarea').value;
        const response = await fetch('/api/generate-ad', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt, format: state.currentFormat }),
        });
        const result = await response.json();
        if (!response.ok || !result.image_url) {
            throw new Error(result && result.error ? result.error : 'Image generation failed.');
        }
        const topic = state.topics.find(t => t.id === state.selectedTopicId) || state.topics[0];
        if (topic) topic.image_url = result.image_url;
        updateCanvasView(topic || { image_url: result.image_url });
        switchAdView('image');
        showToast(`Image generated (${result.aspect_ratio}).`);
    } catch (err) {
        console.error('Image generation error:', err);
        showToast(err instanceof Error ? err.message : 'Image generation failed.', 'error');
    } finally {
        state.isGenerating = false;
        btn.disabled = false;
        spinner.classList.add('hidden');
    }
}

/**
 * Dropdown Handlers
 */
function onThemeChange() {
    const selector = document.getElementById('theme-selector');
    state.currentTheme = selector.value;
    const canvas = document.getElementById('ad-graphic-canvas');
    
    if (state.currentTheme.includes('Matrix')) {
        canvas.style.background = 'radial-gradient(circle at 60% 30%, #062b1a 0%, #020d07 100%)';
    } else if (state.currentTheme.includes('Purple')) {
        canvas.style.background = 'radial-gradient(circle at 60% 30%, #220e40 0%, #0a0414 100%)';
    } else if (state.currentTheme.includes('Slate')) {
        canvas.style.background = 'radial-gradient(circle at 60% 30%, #1e293b 0%, #0f172a 100%)';
    } else {
        canvas.style.background = 'radial-gradient(circle at 60% 30%, #0d2138 0%, #040a16 100%)';
    }
    showToast(`Theme updated to ${state.currentTheme}`);
}

function onFormatChange() {
    const selector = document.getElementById('format-selector');
    state.currentFormat = selector.value;
    showToast(`Ad format adjusted for ${state.currentFormat}`);
}

/**
 * Copy Ad Text to Clipboard
 */
function copyAdText() {
    const headline = document.getElementById('copy-headline').textContent;
    const body = document.getElementById('copy-body').textContent;
    const hashtags = document.getElementById('copy-hashtags').textContent;
    const cta = document.getElementById('copy-cta-text').textContent;

    const fullText = `${headline}\n\n${body}\n\n${hashtags}\n\nCTA: ${cta}`;

    navigator.clipboard.writeText(fullText).then(() => {
        showToast('📋 Ad text copied to clipboard!');
    }).catch(() => {
        showToast('Failed to copy to clipboard', 'error');
    });
}

/**
 * Forward / Share Ad
 */
function shareAd() {
    const headline = document.getElementById('copy-headline').textContent;
    const shareData = {
        title: headline,
        text: document.getElementById('copy-body').textContent,
        url: window.location.href
    };

    if (navigator.share) {
        navigator.share(shareData).catch(err => console.log('Share dismissed:', err));
    } else {
        copyAdText();
        showToast('🔗 Shareable ad content copied to clipboard!');
    }
}

/**
 * Re-roll Ad
 */
function rerollAd() {
    generateAdFromPrompt();
}

/**
 * Scroll to top URL input section
 */
function scrollToUrlInput() {
    const section = document.getElementById('section-url-input');
    if (section) {
        section.scrollIntoView({ behavior: 'smooth' });
        const textarea = document.getElementById('url-input-textarea');
        if (textarea) textarea.focus();
    }
}

/**
 * Toast Notification Utility
 */
function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    const toastMsg = document.getElementById('toast-message');
    if (!toast || !toastMsg) return;

    toastMsg.textContent = message;
    toast.classList.remove('hidden');

    setTimeout(() => {
        toast.classList.add('hidden');
    }, 3500);
}

/**
 * HTML Escape Helper
 */
function escapeHtml(str) {
    if (!str) return '';
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
