(() => {
  'use strict';

  const appConfig = window.APP_CONFIG || { sentences: [], paragraphs: [] };
  const sentences = Array.isArray(appConfig.sentences) ? appConfig.sentences : [];
  const paragraphs = Array.isArray(appConfig.paragraphs) ? appConfig.paragraphs : [];

  const contentTypeInputs = document.querySelectorAll('input[name="contentType"]');
  const nextContentBtn = document.getElementById('nextContent');
  const targetContentEl = document.getElementById('targetContent');
  const contentHeadingEl = document.getElementById('contentHeading');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const statusMessage = document.getElementById('statusMessage');
  const resultsSection = document.getElementById('results');
  const transcriptText = document.getElementById('transcriptText');
  const scoreValue = document.getElementById('scoreValue');

  if (!targetContentEl || !startBtn || !stopBtn || !statusMessage) {
    return;
  }

  const contentCollections = {
    sentence: sentences,
    paragraph: paragraphs,
  };

  const indices = { sentence: 0, paragraph: 0 };
  let currentType = sentences.length ? 'sentence' : (paragraphs.length ? 'paragraph' : 'sentence');
  let selectedContentId = null;

  function setRadioState() {
    contentTypeInputs.forEach((input) => {
      input.checked = input.value === currentType;
    });
  }

  function resetResults() {
    if (resultsSection) {
      resultsSection.hidden = true;
    }
    if (transcriptText) {
      transcriptText.textContent = '-';
    }
    if (scoreValue) {
      scoreValue.textContent = '0%';
    }
  }

  function updateContentDisplay(indexOverride) {
    const list = contentCollections[currentType] || [];
    if (!list.length) {
      selectedContentId = null;
      if (contentHeadingEl) {
        contentHeadingEl.textContent = currentType === 'paragraph' ? 'Practice Paragraph' : 'Practice Sentence';
      }
      targetContentEl.textContent = 'No practice content available.';
      startBtn.disabled = true;
      if (nextContentBtn) {
        nextContentBtn.disabled = true;
      }
      resetResults();
      return;
    }

    startBtn.disabled = false;
    if (nextContentBtn) {
      nextContentBtn.disabled = false;
    }
    const nextIndex = typeof indexOverride === 'number' ? indexOverride : indices[currentType] || 0;
    const normalizedIndex = ((nextIndex % list.length) + list.length) % list.length;
    indices[currentType] = normalizedIndex;

    const currentItem = list[normalizedIndex];
    selectedContentId = currentItem.id;
    targetContentEl.textContent = currentItem.text;
    if (contentHeadingEl) {
      contentHeadingEl.textContent = currentType === 'paragraph' ? 'Practice Paragraph' : 'Practice Sentence';
    }
    resetResults();
  }

  setRadioState();
  updateContentDisplay(indices[currentType] || 0);

  contentTypeInputs.forEach((input) => {
    input.addEventListener('change', (event) => {
      if (!event.target.checked) {
        return;
      }
      currentType = event.target.value === 'paragraph' ? 'paragraph' : 'sentence';
      if (!contentCollections[currentType].length) {
        indices[currentType] = 0;
      }
      updateContentDisplay(indices[currentType] || 0);
    });
  });

  if (nextContentBtn) {
    nextContentBtn.addEventListener('click', () => {
      const list = contentCollections[currentType] || [];
      if (!list.length) {
        return;
      }
      const nextIndex = (indices[currentType] + 1) % list.length;
      updateContentDisplay(nextIndex);
    });
  }

  let mediaRecorder = null;
  let audioChunks = [];
  let isRecording = false;

  function resetControls() {
    startBtn.disabled = false;
    stopBtn.disabled = true;
    mediaRecorder = null;
    audioChunks = [];
  }

  async function startRecording() {
    if (!navigator.mediaDevices || typeof MediaRecorder === 'undefined') {
      statusMessage.textContent = 'Recording is not supported in this browser.';
      return;
    }
    if (!selectedContentId) {
      statusMessage.textContent = 'Select a practice item before recording.';
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];

      mediaRecorder.addEventListener('dataavailable', (event) => {
        if (event.data && event.data.size > 0) {
          audioChunks.push(event.data);
        }
      });

      mediaRecorder.addEventListener('stop', () => handleStop(stream));

      mediaRecorder.start();
      isRecording = true;
      startBtn.disabled = true;
      stopBtn.disabled = false;
      statusMessage.textContent = 'Recording...';
    } catch (error) {
      statusMessage.textContent = `Microphone access denied: ${error.message || error}`;
    }
  }

  function stopRecording() {
    if (!mediaRecorder || !isRecording) {
      return;
    }

    stopBtn.disabled = true;
    statusMessage.textContent = 'Processing audio...';
    mediaRecorder.stop();
    isRecording = false;
  }

  async function handleStop(stream) {
    stream.getTracks().forEach((track) => track.stop());

    if (audioChunks.length === 0) {
      statusMessage.textContent = 'No audio captured.';
      resetControls();
      return;
    }

    const mimeType = mediaRecorder && mediaRecorder.mimeType ? mediaRecorder.mimeType : 'audio/webm';
    const audioBlob = new Blob(audioChunks, { type: mimeType });

    try {
      await submitAudio(audioBlob);
    } catch (error) {
      statusMessage.textContent = error.message;
    } finally {
      resetControls();
    }
  }

  async function submitAudio(audioBlob) {
    if (!selectedContentId) {
      throw new Error('No practice content selected.');
    }

    const formData = new FormData();
    formData.append('audio', audioBlob, 'practice.webm');
    formData.append('contentId', String(selectedContentId));
    formData.append('contentType', currentType);
    if (currentType === 'sentence') {
      formData.append('sentenceId', String(selectedContentId));
    }

    const response = await fetch('/transcribe', {
      method: 'POST',
      body: formData,
    });

    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      throw new Error('Unable to parse response from server.');
    }

    if (response.status === 401) {
      statusMessage.textContent = 'Session expired. Redirecting to sign in...';
      window.location.href = '/login';
      return;
    }

    if (!response.ok) {
      throw new Error(payload.error || 'Transcription failed.');
    }

    transcriptText.textContent = payload.transcript || '(no transcription)';
    const numericScore = typeof payload.score === 'number' ? payload.score : Number(payload.score);
    const safeScore = Number.isFinite(numericScore) ? numericScore.toFixed(2) : '0.00';
    scoreValue.textContent = `${safeScore}%`;
    resultsSection.hidden = false;
    statusMessage.textContent = 'Transcription complete.';
  }

  startBtn.addEventListener('click', startRecording);
  stopBtn.addEventListener('click', stopRecording);
})();
