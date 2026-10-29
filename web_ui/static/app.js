const state = {
  runId: null,
  topics: [],
  selectedTopic: null,
  campaign: null,
  imageUrl: null,
  theme: "Midnight cyan",
  platform: "LinkedIn",
};

const $ = (selector) => document.querySelector(selector);
const elements = {
  urlInput: $("#url-input"),
  urlCount: $("#url-count"),
  discoveryQuery: $("#discovery-query"),
  stage: $("#stage-select"),
  scrape: $("#scrape-button"),
  discover: $("#discover-button"),
  sourceError: $("#source-error"),
  topicsSection: $("#topics-section"),
  topicList: $("#topic-list"),
  topicCount: $("#topic-count"),
  sourceStatus: $("#source-status"),
  composer: $("#composer-section"),
  selectedTopic: $("#selected-topic"),
  theme: $("#theme-select"),
  platform: $("#platform-select"),
  stageTwo: $("#stage-select-2"),
  creativeAngle: $("#creative-angle"),
  compose: $("#compose-button"),
  workspace: $("#composer-workspace"),
  imagePrompt: $("#image-prompt"),
  llmPrompt: $("#llm-prompt"),
  copyPreview: $("#copy-preview"),
  composeError: $("#compose-error"),
  render: $("#render-button"),
  final: $("#final-section"),
  canvas: $("#ad-canvas"),
  canvasLoading: $("#canvas-loading"),
  toast: $("#toast"),
};

const palette = {
  "Midnight cyan": { background: "#091424", accent: "#31d5f2", accentDark: "#146e9b", glow: "#1b82b4" },
  "Warm editorial": { background: "#201a18", accent: "#ffca8b", accentDark: "#b96844", glow: "#9e5c42" },
  "Emerald lab": { background: "#0a201c", accent: "#54e2b0", accentDark: "#19785e", glow: "#158a69" },
  "Ultraviolet research": { background: "#17152b", accent: "#b2a0ff", accentDark: "#6253be", glow: "#6851cc" },
};

async function api(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request could not be completed.");
  return result;
}

function setBusy(button, busy, label) {
  if (busy) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="loader" aria-hidden="true"></span><span>${label}</span>`;
  } else {
    button.disabled = false;
    if (button.dataset.label) button.innerHTML = button.dataset.label;
  }
}

function showError(element, message) {
  element.textContent = message;
  element.hidden = false;
}

function clearError(element) {
  element.hidden = true;
  element.textContent = "";
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("is-visible");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 2300);
}

function scrollTo(element) {
  element.scrollIntoView({ behavior: "smooth", block: "start" });
}

function updateStep(step) {
  document.querySelectorAll(".nav-step").forEach((item) => {
    const number = Number(item.dataset.step);
    item.classList.toggle("is-active", number === step);
    item.classList.toggle("is-done", number < step);
    item.disabled = number > step;
  });
}

function parseUrls() {
  return elements.urlInput.value
    .split(/\r?\n/)
    .map((url) => url.trim().replace(/^["']|["']$/g, "").replace(/,$/, "").trim())
    .filter(Boolean);
}

elements.urlInput.addEventListener("input", () => {
  const count = parseUrls().length;
  elements.urlCount.textContent = `${count} ${count === 1 ? "link" : "links"} added`;
});

async function findTopics() {
  clearError(elements.sourceError);
  const urls = parseUrls();
  if (!urls.length) {
    showError(elements.sourceError, "Paste at least one public webpage URL to get started.");
    return;
  }
  if (urls.length > 8) {
    showError(elements.sourceError, "This run supports up to 8 URLs. Remove a few and try again.");
    return;
  }
  setBusy(elements.scrape, true, "Reading your sources…");
  try {
    const result = await api("/api/topics", { urls });
    showTopics(result);
  } catch (error) {
    showError(elements.sourceError, error.message);
  } finally {
    setBusy(elements.scrape, false);
  }
}

async function discoverTopics() {
  clearError(elements.sourceError);
  const queries = elements.discoveryQuery.value
    .split(/[,\r\n]+/)
    .map((query) => query.trim())
    .filter(Boolean);
  if (queries.length > 12) {
    showError(elements.sourceError, "Use no more than 12 search topics.");
    return;
  }
  setBusy(elements.discover, true, "Searching public sources…");
  try {
    const result = await api("/api/discover", { queries });
    showTopics(result);
  } catch (error) {
    showError(elements.sourceError, error.message);
  } finally {
    setBusy(elements.discover, false);
  }
}

function showTopics(result) {
  state.runId = result.run_id;
  state.topics = result.topics;
  state.selectedTopic = result.topics[0] || null;
  elements.compose.disabled = false;
  elements.render.disabled = false;
  elements.theme.disabled = false;
  elements.platform.disabled = false;
  elements.stageTwo.disabled = false;
  $("#reroll-button").disabled = false;
  renderTopics(result);
  elements.topicsSection.hidden = false;
  elements.composer.hidden = true;
  elements.final.hidden = true;
  updateStep(2);
  scrollTo(elements.topicsSection);
}

function renderTopics(result) {
  elements.topicCount.textContent = `${result.topics.length} ${result.topics.length === 1 ? "TOPIC" : "TOPICS"}`;
  elements.sourceStatus.replaceChildren();
  if (result.discovery) {
    const summary = document.createElement("span");
    summary.className = "source-summary";
    const counts = Object.entries(result.discovery.record_counts || {})
      .map(([platform, count]) => `${platform}: ${count}`)
      .join(" · ");
    summary.textContent = `Automatic discovery · ${counts || "records collected"} · ${result.discovery.stats?.["requests finished"] || 0} requests completed`;
    elements.sourceStatus.append(summary);
  }
  result.sources.forEach((source) => {
    const chip = document.createElement("span");
    chip.className = `source-chip${source.status === "failed" ? " failed" : ""}`;
    const platform = source.platform ? `${source.platform}: ` : "";
    chip.textContent = `${source.status === "success" ? "✓ " : "↗ "}${platform}${source.title || source.url}${source.status === "failed" ? " · skipped" : ""}`;
    chip.title = source.status === "failed" ? source.error : source.url;
    elements.sourceStatus.append(chip);
  });
  elements.topicList.replaceChildren();
  result.topics.forEach((topic, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `topic-card${index === 0 ? " is-selected" : ""}`;
    card.setAttribute("aria-pressed", index === 0 ? "true" : "false");

    const topline = document.createElement("div");
    topline.className = "topic-topline";
    const rank = document.createElement("span");
    rank.className = "topic-rank";
    rank.textContent = `ANGLE 0${index + 1}`;
    const score = document.createElement("span");
    score.className = "topic-score";
    score.textContent = Number.isFinite(topic.score) ? `${Math.round(topic.score)} SIGNAL` : `${topic.source_count} SOURCE${topic.source_count === 1 ? "" : "S"}`;
    topline.append(rank, score);

    const title = document.createElement("h3");
    title.textContent = topic.title;
    const summary = document.createElement("p");
    summary.textContent = topic.summary;
    const foot = document.createElement("div");
    foot.className = "topic-card-foot";
    foot.textContent = `${topic.source_count} SOURCE${topic.source_count === 1 ? "" : "S"} · ${topic.platforms.join(" / ") || "WEBPAGE"}`;
    card.append(topline, title, summary, foot);
    card.addEventListener("click", () => selectTopic(topic));
    elements.topicList.append(card);
  });
}

function selectTopic(topic) {
  state.selectedTopic = topic;
  document.querySelectorAll(".topic-card").forEach((card) => {
    const selected = card.querySelector("h3")?.textContent === topic.title;
    card.classList.toggle("is-selected", selected);
    card.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  openComposer();
}

async function openComposer() {
  if (!state.selectedTopic) return;
  elements.composer.hidden = false;
  elements.workspace.hidden = true;
  elements.selectedTopic.replaceChildren();
  const label = document.createElement("span");
  label.textContent = "SELECTED RESEARCH ANGLE";
  const title = document.createElement("b");
  title.textContent = state.selectedTopic.title;
  elements.selectedTopic.append(label, title);
  clearError(elements.composeError);
  updateStep(2);
  scrollTo(elements.composer);
  await loadPromptPreview();
}

async function loadPromptPreview() {
  if (!state.runId || !state.selectedTopic) return;
  const original = elements.compose.textContent;
  elements.compose.disabled = true;
  elements.compose.textContent = "Preparing prompt…";
  try {
    const response = await api("/api/prompt-preview", {
      run_id: state.runId,
      trend_id: state.selectedTopic.trend_id,
      stage: elements.stageTwo.value,
      creative_angle: elements.creativeAngle.value,
      theme: elements.theme.value,
      platform: elements.platform.value,
    });
    elements.llmPrompt.value = response.generation_prompt;
  } catch (error) {
    showError(elements.composeError, error.message);
  } finally {
    elements.compose.disabled = false;
    elements.compose.innerHTML = `<span>✦</span> Draft with Gemini`;
    elements.compose.dataset.label = original;
  }
}

async function composeCampaign() {
  clearError(elements.composeError);
  if (!elements.llmPrompt.value.trim()) {
    showError(elements.composeError, "The Gemini prompt is empty. Choose the topic again to prepare a prompt.");
    return;
  }
  setBusy(elements.compose, true, "Gemini is shaping the story…");
  try {
    const result = await api("/api/compose", {
      run_id: state.runId,
      trend_id: state.selectedTopic.trend_id,
      stage: elements.stageTwo.value,
      creative_angle: elements.creativeAngle.value,
      theme: elements.theme.value,
      platform: elements.platform.value,
      generation_prompt: elements.llmPrompt.value,
    });
    state.campaign = result;
    state.theme = result.theme;
    state.platform = result.platform;
    elements.imagePrompt.value = result.post.image_prompt;
    renderCopy(result.post);
    elements.workspace.hidden = false;
    elements.final.hidden = true;
    updateStep(2);
    scrollTo(elements.workspace);
  } catch (error) {
    showError(elements.composeError, error.message);
  } finally {
    setBusy(elements.compose, false);
  }
}

function renderCopy(post) {
  const heading = document.createElement("h4");
  heading.textContent = post.hook;
  const body = document.createElement("p");
  body.textContent = post.long_version;
  const tags = document.createElement("div");
  tags.className = "copy-tags";
  tags.textContent = (post.hashtags || []).map((tag) => tag.startsWith("#") ? tag : `#${tag}`).join("  ");
  elements.copyPreview.replaceChildren(heading, body, tags);
}

async function renderCampaign() {
  clearError(elements.composeError);
  if (!state.campaign || !state.runId) {
    showError(elements.composeError, "Draft the campaign with Gemini before generating the image.");
    return;
  }
  setBusy(elements.render, true, "Generating the image…");
  try {
    const result = await api("/api/generate-image", {
      run_id: state.runId,
      image_prompt: elements.imagePrompt.value,
    });
    if (!result.image_url) throw new Error("No image was returned.");
    state.campaign.model = `${result.model || "Cloudflare Workers AI"} · ${result.aspect_ratio || ""}`.trim();
    // Cache-bust so a regenerated image replaces the previous one.
    state.imageUrl = `${result.image_url}?t=${Date.now()}`;
    await showFinal(state.imageUrl);
  } catch (error) {
    showError(elements.composeError, error.message);
  } finally {
    setBusy(elements.render, false);
  }
}

function imageReady(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = reject;
    image.src = url;
  });
}

function wrapLines(context, text, maxWidth) {
  const words = String(text || "").split(/\s+/);
  const lines = [];
  let line = "";
  words.forEach((word) => {
    const attempt = line ? `${line} ${word}` : word;
    if (line && context.measureText(attempt).width > maxWidth) {
      lines.push(line);
      line = word;
    } else {
      line = attempt;
    }
  });
  if (line) lines.push(line);
  return lines;
}

function drawWrapped(context, text, x, y, maxWidth, lineHeight, maxLines) {
  let lines = wrapLines(context, text, maxWidth);
  if (maxLines && lines.length > maxLines) {
    lines = lines.slice(0, maxLines);
    let last = lines[lines.length - 1];
    while (last.length && context.measureText(`${last}…`).width > maxWidth) last = last.slice(0, -1);
    lines[lines.length - 1] = `${last}…`;
  }
  lines.forEach((line, index) => context.fillText(line, x, y + index * lineHeight));
  return lines.length * lineHeight;
}

async function drawCreative(imageUrl, campaign, theme) {
  const canvas = elements.canvas;
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  const colors = palette[theme] || palette["Midnight cyan"];
  let image = null;
  try {
    image = await imageReady(imageUrl);
  } catch {
    // The generated image is optional for the layout; retain a designed fallback background.
  }

  ctx.fillStyle = colors.background;
  ctx.fillRect(0, 0, W, H);
  if (image) {
    const scale = Math.max(W / image.width, H / image.height);
    const width = image.width * scale;
    const height = image.height * scale;
    ctx.globalAlpha = 0.49;
    ctx.drawImage(image, (W - width) / 2, (H - height) / 2, width, height);
    ctx.globalAlpha = 1;
  }
  const background = ctx.createLinearGradient(0, 0, W, H);
  background.addColorStop(0, `${colors.background}f5`);
  background.addColorStop(.57, `${colors.background}e6`);
  background.addColorStop(1, `${colors.glow}b0`);
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, W, H);
  ctx.globalAlpha = .16;
  ctx.strokeStyle = "#b3d9ff";
  ctx.lineWidth = 1;
  for (let x = 0; x < W; x += 54) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
  for (let y = 0; y < H; y += 54) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  ctx.globalAlpha = 1;

  ctx.strokeStyle = `${colors.accent}bd`;
  ctx.lineWidth = 2;
  ctx.strokeRect(43, 43, W - 86, H - 86);
  ctx.fillStyle = colors.accent;
  ctx.fillRect(43, 43, 8, 95);
  ctx.font = "600 21px Consolas, monospace";
  ctx.letterSpacing = "2px";
  ctx.fillText("R2C.AI   /   RESEARCH, IN CONTEXT", 83, 105);

  const trendTitle = campaign.topic_title || state.selectedTopic?.title || "Research, put to work";
  ctx.font = "500 23px 'Segoe UI', sans-serif";
  ctx.letterSpacing = "1px";
  ctx.fillStyle = "rgba(237,245,255,.77)";
  ctx.fillText("FIELD NOTE     /     " + String(campaign.stage || "action").toUpperCase(), 83, 174);

  ctx.font = "800 75px 'Segoe UI', sans-serif";
  ctx.letterSpacing = "-3px";
  ctx.fillStyle = "#f7f9ff";
  let titleHeight = drawWrapped(ctx, campaign.post.hook || trendTitle, 83, 285, 840, 91, 3);
  const bodyStart = Math.min(650, 320 + titleHeight + 26);
  ctx.font = "400 29px 'Segoe UI', sans-serif";
  ctx.letterSpacing = "0px";
  ctx.fillStyle = "rgba(222,232,247,.84)";
  drawWrapped(ctx, campaign.post.short_version || campaign.post.long_version, 87, bodyStart, 810, 43, 3);

  const cta = campaign.post.aida_stage === "action"
    ? "EXPLORE THE RESEARCH"
    : campaign.post.aida_stage === "awareness"
      ? "A SIGNAL WORTH FOLLOWING"
      : campaign.post.aida_stage === "interest"
        ? "FOLLOW THE EVIDENCE"
        : "IMAGINE WHAT COMES NEXT";
  const pillY = 875;
  ctx.font = "700 22px 'Segoe UI', sans-serif";
  const pillWidth = Math.min(500, ctx.measureText(cta).width + 74);
  ctx.fillStyle = colors.accent;
  ctx.beginPath();
  ctx.roundRect(83, pillY, pillWidth, 66, 16);
  ctx.fill();
  ctx.fillStyle = colors.background;
  ctx.fillText(cta, 109, pillY + 42);

  ctx.font = "500 19px Consolas, monospace";
  ctx.letterSpacing = "1px";
  ctx.fillStyle = "rgba(220,232,248,.64)";
  ctx.fillText("ACADEMIC INSIGHT   →   INDUSTRY POSSIBILITY", 83, 1006);
  ctx.textAlign = "right";
  ctx.fillStyle = colors.accent;
  ctx.fillText("R2C.AI", 995, 1006);
  ctx.textAlign = "left";
  ctx.letterSpacing = "0px";
}

async function showFinal(imageUrl) {
  elements.final.hidden = false;
  elements.canvasLoading.hidden = false;
  elements.finalTitle.textContent = state.campaign.topic_title;
  elements.finalDescription.textContent = state.campaign.post.hook;
  elements.finalPlatform.textContent = state.campaign.platform;
  elements.finalModel.textContent = state.campaign.model || "Generated image";
  const tags = (state.campaign.post.hashtags || [])
    .map((tag) => tag.startsWith("#") ? tag : `#${tag}`)
    .join(" ");
  elements.finalCaption.textContent = `${state.campaign.post.hook}\n\n${state.campaign.post.long_version}${tags ? `\n\n${tags}` : ""}`;
  await drawCreative(imageUrl, state.campaign, state.theme);
  elements.canvasLoading.hidden = true;
  updateStep(3);
  scrollTo(elements.final);
}

elements.finalTitle = $("#final-title");
elements.finalDescription = $("#final-description");
elements.finalPlatform = $("#final-platform");
elements.finalModel = $("#final-model");
elements.finalCaption = $("#final-caption");

elements.scrape.addEventListener("click", findTopics);
elements.discover.addEventListener("click", discoverTopics);
elements.compose.addEventListener("click", composeCampaign);
elements.render.addEventListener("click", renderCampaign);
elements.stageTwo.addEventListener("change", loadPromptPreview);
elements.creativeAngle.addEventListener("change", loadPromptPreview);
elements.theme.addEventListener("change", loadPromptPreview);
elements.platform.addEventListener("change", loadPromptPreview);
$("#change-topic").addEventListener("click", () => scrollTo(elements.topicsSection));
$("#start-over").addEventListener("click", () => {
  state.runId = null;
  state.selectedTopic = null;
  state.campaign = null;
  elements.urlInput.value = "";
  elements.urlCount.textContent = "0 links added";
  elements.discoveryQuery.value = "";
  elements.topicsSection.hidden = true;
  elements.composer.hidden = true;
  elements.final.hidden = true;
  updateStep(1);
  scrollTo($("#intro"));
});

elements.theme.addEventListener("change", async () => {
  if (state.selectedTopic) await loadPromptPreview();
});
elements.platform.addEventListener("change", async () => {
  if (state.selectedTopic) await loadPromptPreview();
});
elements.stageTwo.addEventListener("change", async () => {
  elements.stage.value = elements.stageTwo.value;
  if (state.selectedTopic) await loadPromptPreview();
});
elements.stage.addEventListener("change", () => { elements.stageTwo.value = elements.stage.value; });

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(message);
  } catch {
    showToast("Clipboard access was blocked by the browser.");
  }
}

$("#copy-button").addEventListener("click", () => {
  if (state.campaign) {
    const post = state.campaign.post;
    const hashtags = (post.hashtags || []).map((tag) => tag.startsWith("#") ? tag : `#${tag}`).join(" ");
    copyText(`${post.hook}\n\n${post.long_version}\n\n${hashtags}`, "Campaign copy copied.");
  }
});
$("#copy-caption").addEventListener("click", () => copyText(elements.finalCaption.textContent, "Caption copied."));
$("#download-button").addEventListener("click", () => {
  const link = document.createElement("a");
  link.download = "r2c-research-campaign.png";
  link.href = elements.canvas.toDataURL("image/png");
  link.click();
});
$("#reroll-button").addEventListener("click", renderCampaign);

document.querySelectorAll(".nav-step").forEach((button) => {
  button.addEventListener("click", () => {
    const step = Number(button.dataset.step);
    const target = { 1: $("#source-section"), 2: elements.topicsSection, 3: elements.final }[step];
    if (target && !target.hidden) scrollTo(target);
  });
});
