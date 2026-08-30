/**
 * Allen Hybrid Review Studio Controller
 * Handles KaTeX + mhchem rendering, SmilesDrawer 2D molecular structures,
 * Cloudinary zero-credit uploads, custom ID prefixes, and clean JSON exports.
 */

let studioData = { metadata: {}, questions: [] };
let activeFilter = 'all';
let searchQuery = '';
let cloudinaryUrlMap = {};

// DOM Elements
const qListContainer = document.getElementById('questions-list');
const navFilename = document.getElementById('nav-filename');
const statTotal = document.getElementById('stat-total');
const statText = document.getElementById('stat-text');
const statCrop = document.getElementById('stat-crop');
const exportFilenameInput = document.getElementById('export-filename');
const idPrefixInput = document.getElementById('id-prefix');
const idPreviewHint = document.getElementById('id-preview-hint');
const defaultMarksInput = document.getElementById('default-marks');
const searchInput = document.getElementById('search-input');

// Test Metadata Modal Elements
const metadataModal = document.getElementById('metadata-modal');
const btnMetadataModal = document.getElementById('btn-metadata-modal');
const btnCloseMetaModal = document.getElementById('btn-close-meta-modal');
const btnCancelMeta = document.getElementById('btn-cancel-meta');
const btnSaveMeta = document.getElementById('btn-save-meta');
const metaTestId = document.getElementById('meta-test-id');
const metaTestName = document.getElementById('meta-test-name');
const metaDisplayName = document.getElementById('meta-display-name');
const metaDescription = document.getElementById('meta-description');
const metaDuration = document.getElementById('meta-duration');
const metaCorrectMarks = document.getElementById('meta-correct-marks');
const metaIncorrectMarks = document.getElementById('meta-incorrect-marks');
const metaSyllabus = document.getElementById('meta-syllabus');

// Cloudinary Modal Elements
const cloudinaryModal = document.getElementById('cloudinary-modal');
const btnCloudinaryModal = document.getElementById('btn-cloudinary-modal');
const btnCloseModal = document.getElementById('btn-close-modal');
const btnCancelCloudinary = document.getElementById('btn-cancel-cloudinary');
const btnSaveCloudinary = document.getElementById('btn-save-cloudinary');
const cloudNameInput = document.getElementById('cloud-name');
const cloudApiKeyInput = document.getElementById('cloud-api-key');
const cloudApiSecretInput = document.getElementById('cloud-api-secret');
const cloudBaseFolderInput = document.getElementById('cloud-base-folder');
const rememberCloudinaryCheck = document.getElementById('remember-cloudinary');

// Export Buttons
const btnExportCleanJson = document.getElementById('btn-export-clean-json');
const btnUploadCloudinaryExport = document.getElementById('btn-upload-cloudinary-export');

// --- Initialization ---
document.addEventListener('DOMContentLoaded', () => {
  loadSavedCloudinaryConfig();
  loadSavedIdPrefix();
  loadSavedTestMetadata();
  initEventListeners();
  fetchStudioData();
});

function initEventListeners() {
  // Filter tabs
  document.querySelectorAll('.filter-tab, .stat-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const f = e.currentTarget.getAttribute('data-filter');
      if (!f) return;
      activeFilter = f;
      document.querySelectorAll('.filter-tab, .stat-btn').forEach(b => {
        if (b.getAttribute('data-filter') === f) b.classList.add('active');
        else b.classList.remove('active');
      });
      renderQuestionCards();
    });
  });

  // Search input
  searchInput.addEventListener('input', (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderQuestionCards();
  });

  // ID Prefix changes
  idPrefixInput.addEventListener('input', () => {
    const val = idPrefixInput.value.trim();
    localStorage.setItem('studio_id_prefix', val);
    updateIdPreviewHint();
    if (!metaTestId.value || metaTestId.value.startsWith('test-') || metaTestId.dataset.auto) {
      metaTestId.value = val.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      metaTestId.dataset.auto = 'true';
    }
  });

  // Metadata Modal Handlers
  if (btnMetadataModal) btnMetadataModal.addEventListener('click', () => metadataModal.classList.add('open'));
  if (btnCloseMetaModal) btnCloseMetaModal.addEventListener('click', () => metadataModal.classList.remove('open'));
  if (btnCancelMeta) btnCancelMeta.addEventListener('click', () => metadataModal.classList.remove('open'));

  if (btnSaveMeta) {
    btnSaveMeta.addEventListener('click', () => {
      saveTestMetadata();
      metadataModal.classList.remove('open');
      alert('Test configuration saved!');
    });
  }

  // Import JSON Modal Handlers
  const importModal = document.getElementById('import-json-modal');
  const btnImportModal = document.getElementById('btn-import-modal');
  const btnCloseImportModal = document.getElementById('btn-close-import-modal');
  const btnCancelImport = document.getElementById('btn-cancel-import');
  const studioImportFile = document.getElementById('studio-import-file');
  const studioImportText = document.getElementById('studio-import-text');
  const btnSubmitStudioImport = document.getElementById('btn-submit-studio-import');

  if (btnImportModal) btnImportModal.addEventListener('click', () => importModal.classList.add('open'));
  if (btnCloseImportModal) btnCloseImportModal.addEventListener('click', () => importModal.classList.remove('open'));
  if (btnCancelImport) btnCancelImport.addEventListener('click', () => importModal.classList.remove('open'));

  if (studioImportFile) {
    studioImportFile.addEventListener('change', (e) => {
      if (e.target.files.length > 0) {
        const file = e.target.files[0];
        const reader = new FileReader();
        reader.onload = (ev) => {
          studioImportText.value = ev.target.result;
        };
        reader.readAsText(file);
      }
    });
  }

  if (btnSubmitStudioImport) {
    btnSubmitStudioImport.addEventListener('click', async () => {
      const raw = (studioImportText.value || '').trim();
      if (!raw) {
        alert('Please paste JSON text or select a JSON file.');
        return;
      }
      try {
        let clean = raw;
        if (clean.startsWith('```')) {
          clean = clean.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/i, '');
        }
        JSON.parse(clean);
      } catch (err) {
        alert('Invalid JSON syntax: ' + err.message);
        return;
      }

      btnSubmitStudioImport.disabled = true;
      btnSubmitStudioImport.textContent = '⏳ Loading...';

      try {
        const res = await fetch('/api/import-json', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ json_text: raw, title: 'Manual Studio Import' })
        });
        const resData = await res.json();
        if (resData.error) throw new Error(resData.error);

        studioData = resData.data;
        setupStudioUI();
        importModal.classList.remove('open');
        btnSubmitStudioImport.disabled = false;
        btnSubmitStudioImport.textContent = '📥 Load Dataset';
        alert(`Successfully imported ${resData.count} questions!`);
      } catch (e) {
        btnSubmitStudioImport.disabled = false;
        btnSubmitStudioImport.textContent = '📥 Load Dataset';
        alert('Failed to import: ' + e.message);
      }
    });
  }

  // Cloudinary Modal Handlers
  btnCloudinaryModal.addEventListener('click', () => cloudinaryModal.classList.add('open'));
  btnCloseModal.addEventListener('click', () => cloudinaryModal.classList.remove('open'));
  btnCancelCloudinary.addEventListener('click', () => cloudinaryModal.classList.remove('open'));

  btnSaveCloudinary.addEventListener('click', () => {
    saveCloudinaryConfig();
    cloudinaryModal.classList.remove('open');
    alert('Cloudinary configuration saved!');
  });

  // Export site JSON
  btnExportCleanJson.addEventListener('click', () => {
    exportCleanJson();
  });

  // Upload to Cloudinary and Export
  btnUploadCloudinaryExport.addEventListener('click', () => {
    uploadToCloudinaryAndExport();
  });
}

function loadSavedTestMetadata() {
  const saved = localStorage.getItem('quizzy_test_metadata');
  if (saved) {
    try {
      const meta = JSON.parse(saved);
      if (meta.id) metaTestId.value = meta.id;
      if (meta.name) metaTestName.value = meta.name;
      if (meta.displayName) metaDisplayName.value = meta.displayName;
      if (meta.description) metaDescription.value = meta.description;
      if (meta.duration) metaDuration.value = meta.duration;
      if (meta.correct_marks) metaCorrectMarks.value = meta.correct_marks;
      if (meta.incorrect_marks) metaIncorrectMarks.value = meta.incorrect_marks;
      if (meta.syllabus_content) metaSyllabus.value = meta.syllabus_content;
    } catch (e) {}
  }
}

function saveTestMetadata() {
  const meta = getTestMetadata();
  localStorage.setItem('quizzy_test_metadata', JSON.stringify(meta));
  return meta;
}

function getTestMetadata() {
  return {
    id: metaTestId.value.trim() || idPrefixInput.value.trim() || 'test-quizzy',
    name: metaTestName.value.trim() || 'MAJOR TEST - 01',
    displayName: metaDisplayName.value.trim() || metaTestName.value.trim() || 'Allen Major Test',
    description: metaDescription.value.trim() || 'Full Syllabus Test',
    duration: parseInt(metaDuration.value, 10) || 180,
    correct_marks: parseInt(metaCorrectMarks.value, 10) || 4,
    incorrect_marks: parseInt(metaIncorrectMarks.value, 10) || -1,
    syllabus_content: metaSyllabus.value.trim()
  };
}

function loadSavedIdPrefix() {
  const saved = localStorage.getItem('studio_id_prefix');
  if (saved) {
    idPrefixInput.value = saved;
  }
  updateIdPreviewHint();
}

function updateIdPreviewHint() {
  const prefix = idPrefixInput.value.trim() || 'prefix';
  idPreviewHint.textContent = `ID: ${prefix}_topic_subtopic_q1`;
}

function loadSavedCloudinaryConfig() {
  const saved = localStorage.getItem('cloudinary_config');
  if (saved) {
    try {
      const cfg = JSON.parse(saved);
      if (cfg.cloud_name) cloudNameInput.value = cfg.cloud_name;
      if (cfg.api_key) cloudApiKeyInput.value = cfg.api_key;
      if (cfg.api_secret) cloudApiSecretInput.value = cfg.api_secret;
      if (cfg.base_folder) cloudBaseFolderInput.value = cfg.base_folder;
    } catch (e) {}
  }
}

function saveCloudinaryConfig() {
  const cfg = {
    cloud_name: cloudNameInput.value.trim(),
    api_key: cloudApiKeyInput.value.trim(),
    api_secret: cloudApiSecretInput.value.trim(),
    base_folder: cloudBaseFolderInput.value.trim() || 'quiz_app'
  };
  if (rememberCloudinaryCheck.checked) {
    localStorage.setItem('cloudinary_config', JSON.stringify(cfg));
  }
  return cfg;
}

function getCloudinaryCredentials() {
  return {
    cloud_name: cloudNameInput.value.trim(),
    api_key: cloudApiKeyInput.value.trim(),
    api_secret: cloudApiSecretInput.value.trim(),
    base_folder: cloudBaseFolderInput.value.trim() || 'quiz_app'
  };
}

// --- Fetch Studio Data ---
async function fetchStudioData() {
  try {
    const res = await fetch('/api/data');
    const data = await res.json();
    if (data && data.questions) {
      studioData = data;
      setupStudioUI();
    }
  } catch (err) {
    console.error('Error fetching data:', err);
    qListContainer.innerHTML = `<div style="text-align:center; padding: 3rem; color: var(--danger);">Failed to load studio data: ${err.message}</div>`;
  }
}

function setupStudioUI() {
  const meta = studioData.metadata || {};
  const filename = meta.filename || 'questions.pdf';
  navFilename.textContent = filename;

  // Prefill default export filename
  const defaultJsonName = filename.replace(/\.pdf$/i, '') + '.json';
  if (!exportFilenameInput.value) {
    exportFilenameInput.value = defaultJsonName;
  }

  updateStats();
  renderQuestionCards();
}

function updateStats() {
  const qs = studioData.questions || [];
  statTotal.textContent = qs.length;
  statText.textContent = qs.filter(q => q.mode === 'text').length;
  statCrop.textContent = qs.filter(q => q.mode === 'crop').length;
}

// --- Render Cards ---
function renderQuestionCards() {
  const qs = studioData.questions || [];
  qListContainer.innerHTML = '';

  const filtered = qs.filter(q => {
    // Mode / Tier Filter
    if (activeFilter === 'tier_text' && q.mode !== 'text') return false;
    if (activeFilter === 'tier_crop' && q.mode !== 'crop') return false;

    // Search filter
    if (searchQuery) {
      const topStr = q.topic || q.top || '';
      const subtopStr = q.subtopic || q.subtop || '';
      const hay = `${q.num} ${q.tag || ''} ${q.subject || q.sub || ''} ${topStr} ${subtopStr} ${q.prompt || q.q || ''} ${(q.options || q.o || []).join(' ')}`.toLowerCase();
      if (!hay.includes(searchQuery)) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    qListContainer.innerHTML = `<div style="text-align:center; padding: 4rem; color: var(--text-muted);">No questions match current filters.</div>`;
    return;
  }

  filtered.forEach(q => {
    const card = document.createElement('div');
    card.className = 'q-card';
    card.id = `card-${q.num}`;

    const isCrop = (q.mode === 'crop');
    const hasImage = !!(q.crop_path || q.image_data_uri || q.image_filename);
    const imgSrc = q.image_data_uri || (q.image_filename ? `/crops/${q.image_filename}` : '');
    const topicName = q.topic || q.top || 'General';
    const subtopicName = q.subtopic || q.subtop || '';

    card.innerHTML = `
      <div class="q-header">
        <div class="q-meta-tags">
          <span class="tag-badge tag-seq">Q${q.sequence || q.num}</span>
          <span class="tag-badge tag-subject">${q.subject || q.sub || 'CHEMISTRY'}</span>
          <span class="tag-badge tag-topic" title="Topic / Chapter">📚 ${escapeHtml(topicName)}</span>
          ${subtopicName ? `<span class="tag-badge tag-subtopic" title="Subtopic / Section">🎯 ${escapeHtml(subtopicName)}</span>` : ''}
          <span class="tag-badge ${isCrop ? 'tag-mode-crop' : 'tag-mode-text'}">
            ${isCrop ? '🖼️ Visual Crop' : '📝 Clean Text'}
          </span>
          ${q.has_diagram ? '<span class="tag-badge tag-mode-crop">📊 Diagram Flagged</span>' : ''}
        </div>
        <div>
          ${hasImage ? `
            <button class="q-toggle-btn" onclick="toggleQuestionMode(${q.num})">
              ${isCrop ? '🔄 Switch to Text' : '🔄 Switch to Crop'}
            </button>
          ` : ''}
        </div>
      </div>

      <div class="q-body ${isCrop && hasImage ? 'has-split' : ''}">
        <div class="q-text-content">
          <div class="q-prompt-box">
            <strong>${q.sequence || q.num}. </strong>
            <span class="latex-render-target">${renderFormattedText(q.prompt || '')}</span>
          </div>

          ${renderMatchListsHtml(q.match_lists || q.matchLists || q.m)}

          ${q.smiles ? `
            <div class="smiles-box">
              <span style="font-size: 0.75rem; color: var(--accent);">🧪 SMILES Structure:</span>
              <canvas id="smiles-canvas-${q.num}" class="smiles-canvas" width="220" height="140"></canvas>
            </div>
          ` : ''}

          <div class="q-options-grid">
            ${(q.options || []).map((opt, optIdx) => `
              <div class="q-option-item ${q.correct_index === optIdx ? 'correct' : ''}">
                <strong>(${optIdx + 1})</strong>
                <span class="latex-render-target">${renderFormattedText(opt || '')}</span>
                ${q.correct_index === optIdx ? '<span style="margin-left:auto;">✓</span>' : ''}
              </div>
            `).join('')}
          </div>

          ${q.solution ? `
            <div class="q-solution-box">
              <strong>Solution: </strong>
              <span class="latex-render-target">${renderFormattedText(q.solution)}</span>
            </div>
          ` : ''}
        </div>

        ${isCrop && hasImage ? `
          <div class="q-crop-preview">
            <span style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.5rem;">Visual Diagram Crop (300 DPI)</span>
            <img src="${imgSrc}" alt="Question Crop" loading="lazy" />
          </div>
        ` : ''}
      </div>
    `;

    qListContainer.appendChild(card);

    // Draw SMILES molecule if present
    if (q.smiles && typeof SmilesDrawer !== 'undefined') {
      setTimeout(() => {
        try {
          const sd = new SmilesDrawer.Drawer({ width: 220, height: 140, compactDrawing: true });
          SmilesDrawer.parse(q.smiles, (tree) => {
            sd.draw(tree, `smiles-canvas-${q.num}`, 'dark', false);
          });
        } catch (e) {
          console.warn('Smiles drawer error:', e);
        }
      }, 50);
    }
  });

  // Render KaTeX and mhchem equations across all targets
  renderLatexMath();
}

function renderMatchListsHtml(matchLists) {
  if (!matchLists || typeof matchLists !== 'object') return '';
  const cols = Object.keys(matchLists);
  if (cols.length === 0) return '';

  return `
    <div class="match-lists-container">
      ${cols.map(colKey => {
        const col = matchLists[colKey] || {};
        const title = col.title || colKey;
        const items = col.items || [];
        return `
          <div class="match-list-col">
            <div class="match-list-title">${renderFormattedText(title)}</div>
            ${items.map(it => `
              <div class="match-list-item">
                <span class="match-list-label">${renderFormattedText(it.label || '')}:</span>
                <span class="latex-render-target">${renderFormattedText(it.text || '')}</span>
              </div>
            `).join('')}
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderFormattedText(str) {
  if (!str) return '';
  let normalized = str.replace(/<br\s*\/?>/gi, '\n');
  let escaped = normalized
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped.replace(/\n/g, '<br />');
}

function escapeHtml(str) {
  return renderFormattedText(str);
}

function renderLatexMath() {
  if (typeof renderMathInElement === 'undefined') return;
  try {
    renderMathInElement(qListContainer, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\ce{', right: '}', display: false }
      ],
      throwOnError: false
    });
  } catch (e) {
    console.warn('KaTeX render error:', e);
  }
}

// Toggle Mode
window.toggleQuestionMode = function(qNum) {
  const q = (studioData.questions || []).find(item => item.num === qNum || item.sequence === qNum);
  if (!q) return;
  q.mode = (q.mode === 'crop') ? 'text' : 'crop';
  updateStats();
  renderQuestionCards();
};

// --- Export Clean JSON ---
async function exportCleanJson() {
  const questions = studioData.questions || [];
  const idPrefix = idPrefixInput.value.trim();
  const filename = exportFilenameInput.value.trim() || 'questions.json';
  const testMeta = getTestMetadata();

  try {
    const res = await fetch('/api/export-json', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        questions: questions,
        id_prefix: idPrefix,
        cloudinary_urls: cloudinaryUrlMap,
        test_metadata: testMeta
      })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    downloadJsonFile(data.json_data, filename);
  } catch (err) {
    alert('Export failed: ' + err.message);
  }
}

// --- Cloudinary Zero-Credit Upload & Export ---
async function uploadToCloudinaryAndExport() {
  const credentials = getCloudinaryCredentials();
  if (!credentials.cloud_name || !credentials.api_key || !credentials.api_secret) {
    cloudinaryModal.classList.add('open');
    alert('Please enter your Cloudinary credentials first.');
    return;
  }

  const questions = studioData.questions || [];
  const cropsToUpload = [];

  questions.forEach(q => {
    if (q.mode === 'crop' && q.crop_path) {
      cropsToUpload.push({
        crop_path: q.crop_path,
        folder_suffix: `${q.topic || 'General'}/${q.exercise_name || 'Questions'}`,
        public_id: `q_${q.num}`
      });
    }
  });

  if (cropsToUpload.length === 0) {
    alert('No diagram crops found in current selection. Exporting clean JSON directly...');
    exportCleanJson();
    return;
  }

  btnUploadCloudinaryExport.disabled = true;
  btnUploadCloudinaryExport.textContent = `⏳ Uploading ${cropsToUpload.length} crops to Cloudinary...`;

  try {
    const res = await fetch('/api/cloudinary-upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        credentials: credentials,
        crops: cropsToUpload
      })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    cloudinaryUrlMap = Object.assign(cloudinaryUrlMap, data.url_map || {});
    alert(`Successfully uploaded ${Object.keys(data.url_map || {}).length} crops to Cloudinary with 0 transformations! Now downloading clean JSON...`);

    exportCleanJson();
  } catch (err) {
    alert('Cloudinary upload error: ' + err.message);
  } finally {
    btnUploadCloudinaryExport.disabled = false;
    btnUploadCloudinaryExport.textContent = '🚀 Cloudinary + JSON';
  }
}

function downloadJsonFile(jsonData, filename) {
  const jsonStr = JSON.stringify(jsonData, null, 2);
  const blob = new Blob([jsonStr], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename.endsWith('.json') ? filename : `${filename}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
