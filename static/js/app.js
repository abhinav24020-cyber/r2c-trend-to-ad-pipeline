/* R2C.Ai Ad Studio: front end for app.py */
"use strict";

const THEME_COLOURS = {
    solar: "#FFD23F", coral: "#FF6F59", lime: "#C6F432",
    cream: "#F6EFE3", mint: "#BFF0DA", night: "#0A1F3C",
};
const FORMAT_SHORT = { "4:5": "Feed", "9:16": "Stories", "1:1": "LinkedIn", "1.91:1": "Link ad" };

let DATA = null;
const state = {
    trend: null, copy: null, picture: null, poster: null, prompt: "", caption: "",
    format: "4:5", theme: "solar", audience: "researchers",
};
let composeTimer = null;
let composeSeq = 0;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 2600);
}

async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* non-JSON error page */ }
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
}

function busy(on, text = "Updating…") {
    $("preview-busy").hidden = !on;
    $("busy-text").textContent = text;
}

/* ---------------------------------------------------------------- render */
function renderStats() {
    const best = (DATA.benchmark || [])[0];
    const stats = [
        [DATA.latest_trends.length, "trends in the latest crawl"],
        [DATA.showcase.length, "ready-to-post ads"],
        [Object.keys(DATA.formats).length, "ad formats"],
        [Object.keys(DATA.themes).length, "colour themes"],
        [best ? `${best.overall}/10` : "-", best ? `best model: ${best.model}` : "model benchmark"],
    ];
    $("hero-stats").innerHTML = stats.map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
}

function renderAds() {
    $("ad-grid").innerHTML = DATA.showcase.map((item, i) => `
        <button class="ad-card" type="button" data-i="${i}" aria-label="Open ad: ${esc(item.copy.headline)}">
            <img src="${esc(item.poster)}" alt="" loading="lazy">
            <div class="ad-card-body">
                <div class="ad-card-title">${esc(item.trend.topic_title)}</div>
                <div class="ad-card-meta">${esc(item.aspect_ratio)} · ${esc(FORMAT_SHORT[item.aspect_ratio] || "")} · ${esc(item.theme)}</div>
            </div>
        </button>`).join("");
    $("ad-grid").querySelectorAll(".ad-card").forEach((el) =>
        el.addEventListener("click", () => openShowcase(DATA.showcase[+el.dataset.i])));
}

function trendCard(t, key) {
    const platforms = (t.platforms || []).map((p) => `<span class="chip">${esc(p)}</span>`).join("");
    const relevance = Math.max(0, Math.min(100, Number(t.r2c_relevance) || 0));
    const source = (t.source_attribution || []).find((s) => s.source_url);
    return `
        <article class="trend">
            <div class="trend-top">
                <h3>${esc(t.topic_title)}</h3>
                <div class="score" title="Composite trend score">${esc(t.score)}<small>score</small></div>
            </div>
            <p>${esc(t.topic_summary || "")}</p>
            <div class="chips">${t.near_miss ? '<span class="chip near">near miss</span>' : ""}${platforms}</div>
            <div>
                <div class="bar-label"><span>R2C relevance</span><span>${relevance ? relevance.toFixed(0) : "-"}</span></div>
                <div class="bar"><i style="width:${relevance}%"></i></div>
            </div>
            <div class="trend-actions">
                <button class="btn btn-primary" type="button" data-open="${esc(key)}">Make an ad</button>
                ${source ? `<a href="${esc(source.source_url)}" target="_blank" rel="noopener">Source ↗</a>` : ""}
            </div>
        </article>`;
}

function renderTrends() {
    const when = DATA.latest_generated_at ? new Date(DATA.latest_generated_at) : null;
    if (when && !isNaN(when)) {
        $("trends-sub").textContent = `Trends from the most recent crawl (${when.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}), ranked by trend score. Near misses scored just under the threshold.`;
    }
    $("trend-list").innerHTML = DATA.latest_trends.map((t, i) => trendCard(t, "latest:" + i)).join("");
    bindTrendButtons($("trend-list"));
}

function bindTrendButtons(root) {
    root.querySelectorAll("[data-open]").forEach((btn) => btn.addEventListener("click", () => {
        const [list, i] = btn.dataset.open.split(":");
        const source = list === "latest" ? DATA.latest_trends : scrapedTrends;
        openTrend(source[+i]);
    }));
}

function renderBench() {
    const rows = (DATA.benchmark || []).map((b, i) => `
        <tr class="${i === 0 ? "win" : ""}">
            <td>${i === 0 ? "🥇 " : ""}${esc(b.model)}</td><td>${esc(b.size)}</td><td>${esc(b.where)}</td>
            <td>${b.adherence}</td><td>${b.realism}</td><td>${b.catchiness}</td><td>${b.artifacts}</td>
            <td><div class="overall"><b>${b.overall}</b><div class="bar"><i style="width:${b.overall * 10}%"></i></div></div></td>
        </tr>`).join("");
    const skipped = (DATA.benchmark_not_tested || []).map((m) => `${esc(m.model)} (${esc(m.size)}): ${esc(m.reason)}`).join(" ");
    $("bench").innerHTML = `
        <table>
            <thead><tr><th>Model</th><th>Size</th><th>Ran on</th><th>Adherence</th><th>Realism</th><th>Catchiness</th><th>Artifacts</th><th>Overall</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <p class="note">Artifacts: 10 = no visible defects. Not tested: ${skipped}</p>`;
}

function renderFormatButtons() {
    $("format-buttons").innerHTML = Object.entries(DATA.formats).map(([ratio, name]) => `
        <button type="button" role="radio" data-format="${esc(ratio)}" aria-checked="false">${esc(ratio)}<small>${esc(name)}</small></button>`).join("");
    $("format-buttons").querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => setFormat(b.dataset.format)));
}

function renderThemes() {
    $("theme-swatches").innerHTML = Object.keys(DATA.themes).map((name) => `
        <button type="button" class="swatch" role="radio" data-theme="${esc(name)}" aria-checked="false" title="${esc(DATA.themes[name])}">
            <i style="background:${THEME_COLOURS[name] || "#ccc"}"></i>${esc(name)}</button>`).join("");
    $("theme-swatches").querySelectorAll(".swatch").forEach((b) =>
        b.addEventListener("click", () => setTheme(b.dataset.theme)));
}

function renderPoints() {
    const options = (DATA.benefits && DATA.benefits[state.audience]) || [];
    const chosen = new Set((state.copy && state.copy.points) || []);
    $("f-points").innerHTML = options.map((p) => `
        <label><input type="checkbox" value="${esc(p)}" ${chosen.has(p) ? "checked" : ""}> ${esc(p)}</label>`).join("");
    $("f-points").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
        const ticked = [...$("f-points").querySelectorAll("input:checked")].map((x) => x.value);
        if (ticked.length > 3) { box.checked = false; toast("Pick up to 3 services."); return; }
        state.copy.points = ticked;
        scheduleCompose();
    }));
}

function syncControls() {
    $("format-buttons").querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.format === state.format)));
    $("theme-swatches").querySelectorAll(".swatch").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.theme === state.theme)));
    $("studio-trend").textContent = state.trend ? state.trend.topic_title : "-";
    const c = state.copy;
    $("copy-fields").disabled = !c;
    $("brief-block").hidden = !!c || !state.trend;
    $("f-eyebrow").value = c ? c.eyebrow || "" : "";
    $("f-headline").value = c ? c.headline || "" : "";
    $("f-subline").value = c ? c.subline || "" : "";
    $("f-sticker").value = c ? c.sticker || "" : "";
    $("f-cta").value = c && c.cta ? c.cta : DATA.ctas[0];
    $("f-prompt").value = state.prompt || "";
    $("btn-picture").disabled = !c;
    renderPoints();
    const hashtags = c && c.hashtags ? "\n\n" + c.hashtags.map((h) => "#" + String(h).replace(/^#/, "")).join(" ") : "";
    $("caption").textContent = c && c.caption ? c.caption + hashtags : "-";
    $("btn-copy-caption").disabled = !(c && c.caption);
}

function showPreview(src, meta) {
    $("preview-img").src = src;
    $("preview-img").hidden = false;
    $("preview-empty").hidden = true;
    const link = $("btn-download");
    link.href = src;
    link.download = `r2c_ad_${state.format.replace(/[:.]/g, "x")}_${state.theme}.jpg`;
    link.setAttribute("aria-disabled", "false");
    $("preview-meta").textContent = meta || "";
}

function clearPreview(message) {
    $("preview-img").hidden = true;
    $("preview-empty").hidden = false;
    $("preview-empty").textContent = message;
    $("btn-download").setAttribute("aria-disabled", "true");
    $("btn-download").removeAttribute("href");
    $("preview-meta").textContent = "";
}

/* ---------------------------------------------------------------- actions */
function openShowcase(item, scroll = true) {
    Object.assign(state, {
        trend: item.trend, copy: JSON.parse(JSON.stringify(item.copy)), picture: item.picture,
        poster: item.poster, prompt: item.image_prompt || "", format: item.aspect_ratio,
        theme: item.theme, audience: item.copy.audience || "researchers",
    });
    syncControls();
    showPreview(item.poster, `${FORMAT_SHORT[item.aspect_ratio] || item.aspect_ratio} · ${item.placement} · picture by ${item.image_model}`);
    if (scroll) $("studio").scrollIntoView({ behavior: "smooth" });
}

function openTrend(trend) {
    Object.assign(state, { trend, copy: null, picture: null, poster: null, prompt: "", audience: "researchers" });
    syncControls();
    clearPreview("Click “Write ad copy with Gemini” to create this ad.");
    $("studio").scrollIntoView({ behavior: "smooth" });
}

function setFormat(ratio) {
    if (state.format === ratio) return;
    state.format = ratio;
    syncControls();
    scheduleCompose(0);
}

function setTheme(name) {
    if (state.theme === name) return;
    state.theme = name;
    // Keep the image prompt's palette in step with the theme for the next generated picture.
    if (state.prompt && DATA.themes[name]) {
        state.prompt = state.prompt.replace(/Colour palette: [^.]*?(dominate[^.]*|, bold and saturated)\./,
            `Colour palette: ${DATA.themes[name]} dominate the whole scene, bold and saturated.`);
    }
    syncControls();
    scheduleCompose(0);
}

function readCopyFields() {
    if (!state.copy) return;
    state.copy.eyebrow = $("f-eyebrow").value;
    state.copy.headline = $("f-headline").value;
    state.copy.subline = $("f-subline").value;
    state.copy.sticker = $("f-sticker").value;
    state.copy.cta = $("f-cta").value;
}

function scheduleCompose(delay = 450) {
    clearTimeout(composeTimer);
    if (!state.copy || !state.picture) return;
    composeTimer = setTimeout(compose, delay);
}

async function compose() {
    readCopyFields();
    if (!state.copy.headline.trim()) { toast("The ad needs a headline.", true); return; }
    const seq = ++composeSeq;
    busy(true, "Laying out the ad…");
    try {
        const result = await api("/api/compose", {
            picture: state.picture, format: state.format, theme: state.theme, copy: state.copy,
        });
        if (seq !== composeSeq) return;  // a newer change is already on its way
        showPreview(result.image, `${FORMAT_SHORT[state.format]} · ${result.size[0]}×${result.size[1]} · ${state.theme} theme`);
    } catch (err) {
        if (seq === composeSeq) toast(err.message, true);
    } finally {
        if (seq === composeSeq) busy(false);
    }
}

async function writeBrief() {
    if (!state.trend) return;
    const btn = $("btn-brief");
    btn.disabled = true;
    busy(true, "Gemini is writing the ad copy…");
    try {
        const result = await api("/api/brief", { trend: state.trend, format: state.format, theme: state.theme });
        state.copy = { ...result.copy, sticker: "" };
        state.audience = result.copy.audience || "researchers";
        state.prompt = result.image_prompt;
        syncControls();
        toast("Ad copy ready. Generating the picture…");
        await generatePicture();
    } catch (err) {
        toast(err.message, true);
        busy(false);
    } finally {
        btn.disabled = false;
    }
}

async function generatePicture() {
    readCopyFields();
    const prompt = $("f-prompt").value.trim();
    if (prompt.length < 40) { toast("Write an image prompt of at least a sentence.", true); return; }
    state.prompt = prompt;
    const btn = $("btn-picture");
    btn.disabled = true;
    busy(true, "Painting the picture (about 10 s)…");
    try {
        const result = await api("/api/generate-picture", { prompt, format: state.format, model: $("f-model").value });
        state.picture = result.picture;
        await compose();
        toast("New picture ready.");
    } catch (err) {
        toast(err.message, true);
        busy(false);
    } finally {
        btn.disabled = !state.copy;
    }
}

let scrapedTrends = [];
async function scrapeLinks() {
    const urls = $("url-input").value.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) { toast("Paste at least one link.", true); return; }
    const btn = $("btn-scrape");
    btn.disabled = true;
    $("scrape-status").textContent = "Scraping and ranking… this can take a minute.";
    try {
        const result = await api("/api/scrape", { urls });
        scrapedTrends = result.trends || [];
        $("scrape-status").textContent = scrapedTrends.length
            ? `${scrapedTrends.length} topic(s) found from ${result.urls.length} link(s).`
            : "No topic passed the quality checks for these links. Try other articles.";
        $("scrape-results").innerHTML = scrapedTrends.map((t, i) => trendCard(t, "scraped:" + i)).join("");
        bindTrendButtons($("scrape-results"));
        if (scrapedTrends.length) $("scrape-results").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
        $("scrape-status").textContent = "";
        toast(err.message, true);
    } finally {
        btn.disabled = false;
    }
}

async function copyCaption() {
    const text = $("caption").textContent;
    try {
        await navigator.clipboard.writeText(text);
        toast("Caption copied.");
    } catch (_) {
        toast("Could not copy. Select the caption text and copy it manually.", true);
    }
}

function fillExampleLinks() {
    // Source links behind the latest crawl's top trends: recent pages that pass Gate 1's freshness check.
    const links = [];
    for (const t of DATA.latest_trends) {
        const s = (t.source_attribution || []).find((x) => /^https?:\/\//.test(x.source_url || ""));
        if (s && !links.includes(s.source_url)) links.push(s.source_url);
        if (links.length === 2) break;
    }
    if (links.length) $("url-input").value = links.join("\n");
}

/* ---------------------------------------------------------------- start */
async function init() {
    try {
        DATA = await api("/api/site-data");
    } catch (err) {
        toast("Could not load the studio data: " + err.message, true);
        return;
    }
    $("f-cta").innerHTML = DATA.ctas.map((c) => `<option>${esc(c)}</option>`).join("");
    renderStats();
    renderAds();
    renderTrends();
    renderBench();
    renderFormatButtons();
    renderThemes();
    syncControls();
    ["f-eyebrow", "f-headline", "f-subline", "f-sticker"].forEach((id) => $(id).addEventListener("input", () => scheduleCompose()));
    $("f-cta").addEventListener("change", () => scheduleCompose(0));
    $("f-prompt").addEventListener("input", () => { state.prompt = $("f-prompt").value; });
    $("btn-brief").addEventListener("click", writeBrief);
    $("btn-picture").addEventListener("click", generatePicture);
    $("btn-scrape").addEventListener("click", scrapeLinks);
    $("btn-copy-caption").addEventListener("click", copyCaption);
    $("btn-examples").addEventListener("click", fillExampleLinks);
    fillExampleLinks();
    if (DATA.showcase.length) openShowcase(DATA.showcase[0], false);  // pre-load the studio, stay at the top
}

document.addEventListener("DOMContentLoaded", init);
