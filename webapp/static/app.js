/* Report Automation — submit form + job status polling. No build step, no deps. */

function initSubmitForm() {
  const form = document.getElementById("job-form");
  const drop = document.getElementById("drop");
  const input = document.getElementById("file-input");
  const chip = document.getElementById("file-chip");
  const chipName = document.getElementById("file-name");
  const clearBtn = document.getElementById("file-clear");
  const nameInput = document.getElementById("report-name");
  const errorBox = document.getElementById("form-error");
  const submitBtn = document.getElementById("submit-btn");
  const spinner = submitBtn.querySelector(".spinner");

  const showError = (msg) => {
    errorBox.textContent = msg;
    errorBox.hidden = !msg;
  };

  const showFile = (file) => {
    if (!file) {
      chip.hidden = true;
      return;
    }
    chipName.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
    chip.hidden = false;
    showError("");
    // Offer the file's own name as the report name, if none typed yet.
    if (!nameInput.value.trim()) {
      nameInput.value = file.name.replace(/\.[^.]+$/, "").slice(0, 80);
    }
  };

  input.addEventListener("change", () => showFile(input.files[0]));

  clearBtn.addEventListener("click", () => {
    input.value = "";
    showFile(null);
  });

  ["dragenter", "dragover"].forEach((evt) =>
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.remove("dragover");
    })
  );
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    showFile(file);
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");

    if (!input.files.length) return showError("Choose a file of links first.");
    if (!nameInput.value.trim()) return showError("Give the report a name.");

    submitBtn.disabled = true;
    spinner.hidden = false;

    const body = new FormData();
    body.append("file", input.files[0]);
    body.append("report_name", nameInput.value.trim());
    body.append("report_type", form.report_type.value);
    body.append("csrf_token", form.csrf_token.value);

    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);
      window.location.href = `/jobs/${data.job_id}`;
    } catch (err) {
      showError(err.message);
      submitBtn.disabled = false;
      spinner.hidden = true;
    }
  });
}

function initJobPage(executionMode) {
  const card = document.getElementById("status-card");
  const jobId = card.dataset.jobId;
  const csrf = card.dataset.csrf;

  const pill = document.getElementById("status-pill");
  const phase = document.getElementById("phase");
  const wrap = document.getElementById("progress-wrap");
  const bar = document.getElementById("progress-bar");
  const counter = document.getElementById("counter");
  const elapsed = document.getElementById("elapsed");
  const errBox = document.getElementById("job-error");
  const downloads = document.getElementById("downloads");
  const cancelForm = document.getElementById("cancel-form");
  const activity = document.getElementById("activity");
  const skippedCard = document.getElementById("skipped-card");
  const skippedList = document.getElementById("skipped");
  const skippedCount = document.getElementById("skipped-count");

  const fmtTime = (s) => {
    const m = Math.floor(s / 60);
    return m ? `${m}m ${s % 60}s` : `${s}s`;
  };

  const renderActivity = (items) => {
    if (!items.length) return;
    activity.innerHTML = "";
    for (const item of items) {
      const li = document.createElement("li");
      li.className = item.level || "info";
      const ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = new Date(item.t * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      li.append(ts, document.createTextNode(item.message));
      activity.append(li);
    }
  };

  const renderSkipped = (items) => {
    if (!items.length) {
      skippedCard.hidden = true;
      return;
    }
    skippedCard.hidden = false;
    skippedCount.textContent = items.length;
    skippedList.innerHTML = "";
    for (const item of items) {
      const li = document.createElement("li");
      const head = document.createElement("strong");
      head.textContent = item.account || item.link || "Unknown post";
      const why = document.createElement("span");
      why.className = "why";
      why.textContent = `${item.reason}${item.account && item.link ? " — " + item.link : ""}`;
      li.append(head, why);
      skippedList.append(li);
    }
  };

  const render = (job) => {
    pill.textContent = job.status;
    pill.className = `status status-${job.status}`;
    phase.textContent = job.phase || "";
    elapsed.textContent = job.elapsed ? fmtTime(job.elapsed) : "";

    const running = job.status === "running" || job.status === "queued";
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    wrap.classList.toggle("indeterminate", running && !job.done);
    wrap.hidden = job.status === "queued" && !job.total;
    bar.style.width = `${job.status === "done" ? 100 : pct}%`;
    counter.textContent = job.total
      ? `${Math.min(job.done, job.total)} / ${job.total} posts captured`
      : "";

    errBox.hidden = !job.error;
    errBox.textContent = job.error || "";

    renderActivity(job.activity || []);
    renderSkipped(job.skipped || []);

    const has = (k) => (job.artifacts || []).includes(k);
    downloads.hidden = !(job.artifacts || []).length;
    for (const kind of ["pdf", "docx", "zip"]) {
      const el = document.getElementById(`dl-${kind}`);
      el.hidden = !has(kind);
      if (has(kind)) el.href = `/api/jobs/${jobId}/download/${kind}`;
    }

    cancelForm.hidden = job.finished;
    document.title = `${job.status} · ${job.name} — Report Automation`;
    return job.finished;
  };

  cancelForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new FormData();
    body.append("csrf_token", csrf);
    const res = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST", body });
    if (res.ok) render(await res.json());
  });

  let delay = 1500;
  const poll = async () => {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (res.status === 401) return (window.location.href = "/login");
      if (!res.ok) throw new Error("status unavailable");
      if (render(await res.json())) return; // terminal state — stop polling
      delay = Math.min(delay * 1.15, 5000); // ease off on long jobs
    } catch (_) {
      delay = Math.min(delay * 2, 15000); // server restarting? back off
    }
    setTimeout(poll, delay);
  };

  // On a scale-to-zero host the container is frozen once the response is sent,
  // so the capture must run inside a request we hold open.
  if (executionMode === "inline") {
    fetch(`/api/jobs/${jobId}/run-inline`).catch(() => {});
  }
  poll();
}
