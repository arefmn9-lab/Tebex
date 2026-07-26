import { Copy, Download, FileJson, FileText, Image, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  diagnosticDownloadUrl,
  listDiagnosticRunFiles,
  listDiagnosticRuns,
  previewDiagnosticFile
} from "../api/automation";

function formatDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("fa-IR", {
    dateStyle: "short",
    timeStyle: "short"
  }).format(new Date(value));
}

function fileIcon(kind) {
  if (kind === "image") return Image;
  if (kind === "json" || kind === "jsonl") return FileJson;
  return FileText;
}

function prettyPreview(filePreview) {
  if (!filePreview) return "";
  if (filePreview.parsed_json) {
    return JSON.stringify(filePreview.parsed_json, null, 2);
  }
  return filePreview.preview || "";
}

export default function Diagnostics() {
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState("");
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const selectedRunSummary = useMemo(
    () => runs.find((run) => run.name === selectedRun),
    [runs, selectedRun]
  );

  async function refreshRuns(preferredRun = selectedRun) {
    setLoadingRuns(true);
    setError("");
    try {
      const payload = await listDiagnosticRuns();
      const nextRuns = payload?.runs || [];
      setRuns(nextRuns);
      const nextSelected = preferredRun && nextRuns.some((run) => run.name === preferredRun)
        ? preferredRun
        : nextRuns[0]?.name || "";
      setSelectedRun(nextSelected);
      if (!nextSelected) {
        setFiles([]);
        setSelectedFile(null);
        setPreview(null);
      }
    } catch (err) {
      setError(err.message || "Failed to load diagnostics.");
    } finally {
      setLoadingRuns(false);
    }
  }

  async function loadFiles(runName) {
    if (!runName) return;
    setLoadingFiles(true);
    setError("");
    try {
      const payload = await listDiagnosticRunFiles(runName);
      const nextFiles = payload?.files || [];
      setFiles(nextFiles);
      setSelectedFile(nextFiles[0] || null);
    } catch (err) {
      setError(err.message || "Failed to load diagnostic files.");
      setFiles([]);
      setSelectedFile(null);
      setPreview(null);
    } finally {
      setLoadingFiles(false);
    }
  }

  async function loadPreview(file) {
    if (!file) return;
    setLoadingPreview(true);
    setError("");
    try {
      const payload = await previewDiagnosticFile(file.relative_path);
      setPreview(payload);
    } catch (err) {
      setPreview(null);
      setError(err.message || "Failed to load diagnostic preview.");
    } finally {
      setLoadingPreview(false);
    }
  }

  async function copyPreview() {
    const text = prettyPreview(preview);
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  useEffect(() => {
    refreshRuns("");
  }, []);

  useEffect(() => {
    loadFiles(selectedRun);
  }, [selectedRun]);

  useEffect(() => {
    loadPreview(selectedFile);
  }, [selectedFile?.relative_path]);

  const previewText = prettyPreview(preview);

  return (
    <section className="diagnostics-page" dir="rtl">
      <div className="page-header">
        <div>
          <h2 className="page-title">گزارش‌های فنی</h2>
          <p className="page-copy">Diagnostic runs and read-only artifacts from backend/runtime/diagnostics.</p>
        </div>
        <button className="secondary-button" onClick={() => refreshRuns()} type="button">
          <RefreshCw size={16} />
          <span>Refresh</span>
        </button>
      </div>

      {error ? <div className="form-error">{error}</div> : null}

      <div className="diagnostics-grid">
        <section className="panel diagnostics-panel">
          <div className="panel-header">
            <h3 className="panel-title">Runs</h3>
            <span className="pill">{loadingRuns ? "Loading" : `${runs.length}`}</span>
          </div>
          <div className="diagnostics-list">
            {runs.length ? runs.map((run) => (
              <button
                className={`diagnostics-list-item ${selectedRun === run.name ? "active" : ""}`}
                key={run.name}
                onClick={() => setSelectedRun(run.name)}
                type="button"
              >
                <strong>{run.name}</strong>
                <span>{formatDate(run.modified_at)}</span>
                <small>{run.file_count} files · {run.screenshot_count} screenshots · {run.status_file || "no status file"}</small>
              </button>
            )) : (
              <div className="empty-state">No diagnostic runs found.</div>
            )}
          </div>
        </section>

        <section className="panel diagnostics-panel">
          <div className="panel-header">
            <h3 className="panel-title">Files</h3>
            <span className="pill">{loadingFiles ? "Loading" : `${files.length}`}</span>
          </div>
          {selectedRunSummary ? (
            <p className="status-note">{selectedRunSummary.name}</p>
          ) : null}
          <div className="diagnostics-list">
            {files.length ? files.map((file) => {
              const Icon = fileIcon(file.kind);
              return (
                <button
                  className={`diagnostics-list-item file ${selectedFile?.relative_path === file.relative_path ? "active" : ""}`}
                  key={file.relative_path}
                  onClick={() => setSelectedFile(file)}
                  type="button"
                >
                  <Icon size={16} />
                  <span>{file.relative_path.split("/").slice(1).join("/") || file.name}</span>
                  <small>{file.kind} · {file.size_bytes.toLocaleString()} bytes</small>
                </button>
              );
            }) : (
              <div className="empty-state">Select a run to browse files.</div>
            )}
          </div>
        </section>

        <section className="panel diagnostics-preview-panel">
          <div className="panel-header">
            <h3 className="panel-title">Preview</h3>
            {selectedFile?.download_supported ? (
              <a className="secondary-button" href={diagnosticDownloadUrl(selectedFile.relative_path)} download>
                <Download size={16} />
                <span>Download</span>
              </a>
            ) : null}
          </div>

          {selectedFile ? (
            <div className="diagnostics-file-meta">
              <strong>{selectedFile.name}</strong>
              <span>{selectedFile.relative_path}</span>
              <span>{formatDate(selectedFile.modified_at)} · {selectedFile.size_bytes.toLocaleString()} bytes</span>
            </div>
          ) : null}

          {loadingPreview ? <div className="empty-state">Loading preview...</div> : null}
          {!loadingPreview && preview?.file?.kind === "image" ? (
            <div className="diagnostics-image-frame">
              <img alt={selectedFile.name} src={diagnosticDownloadUrl(selectedFile.relative_path)} />
            </div>
          ) : null}
          {!loadingPreview && preview?.file?.kind === "unsupported" ? (
            <div className="empty-state">Preview is not supported for this file type.</div>
          ) : null}
          {!loadingPreview && previewText ? (
            <>
              <div className="diagnostics-preview-actions">
                <button className="secondary-button" onClick={copyPreview} type="button">
                  <Copy size={16} />
                  <span>{copied ? "Copied" : "Copy"}</span>
                </button>
                {preview.truncated ? <span className="status-note">Preview truncated for size.</span> : null}
                {preview.parse_error ? <span className="form-error">{preview.parse_error}</span> : null}
              </div>
              <pre className="diagnostics-file-preview" dir="ltr">{previewText}</pre>
            </>
          ) : null}
          {!loadingPreview && !selectedFile ? (
            <div className="empty-state">Select a file to preview diagnostics.</div>
          ) : null}
        </section>
      </div>
    </section>
  );
}
