// Main JavaScript for the Media Search Platform

document.addEventListener('DOMContentLoaded', function() {
    // Mobile menu toggle
    const mobileMenuButton = document.querySelector('.mobile-menu-button');
    const mobileMenu = document.getElementById('mobile-menu');

    if (mobileMenuButton && mobileMenu) {
        mobileMenuButton.addEventListener('click', function() {
            mobileMenu.classList.toggle('hidden');
        });
    }

    // Progress bar updates for media processing
    initializeProgressBars();

    // Initialize file upload behavior
    initializeFileUpload();

    // Initialize tag input
    initializeTagInput();
});

// Function to initialize progress bars and start polling for status updates
function initializeProgressBars() {
    const progressBars = document.querySelectorAll('[data-progress-media-id]');

    progressBars.forEach(function(progressBar) {
        const mediaId = progressBar.dataset.progressMediaId;
        const statusElement = document.querySelector(`[data-status-media-id="${mediaId}"]`);
        const progressElement = progressBar.querySelector('.progress-bar-inner');

        if (mediaId && progressElement) {
            // Start polling for updates if the media is processing
            const status = progressBar.dataset.status;
            if (status === 'pending' || status === 'processing') {
                pollMediaProgress(mediaId, progressElement, statusElement);
            }
        }
    });
}

// Function to poll for media processing progress
function pollMediaProgress(mediaId, progressElement, statusElement) {
    const checkProgress = async () => {
        try {
            const response = await fetch(`/api/v1/media/${mediaId}/progress`);
            if (!response.ok) {
                throw new Error('Failed to fetch progress');
            }

            const data = await response.json();

            // Update progress bar
            progressElement.style.width = `${data.progress}%`;

            // Update status text if available
            if (statusElement) {
                statusElement.textContent = data.status;
            }

            // Continue polling if not complete
            if (!data.completed) {
                setTimeout(checkProgress, 2000); // Poll every 2 seconds
            } else if (data.status === 'completed') {
                // Refresh the page when complete to show the processed media
                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            }
        } catch (error) {
            console.error('Error checking progress:', error);
            setTimeout(checkProgress, 5000); // Retry after 5 seconds on error
        }
    };

    // Start polling
    checkProgress();
}

// Function to initialize file upload behavior
function initializeFileUpload() {
    const fileInput = document.getElementById('file-upload');
    const fileNameDisplay = document.getElementById('file-name');
    const fileForm = document.getElementById('upload-form');
    const uploadButton = document.getElementById('upload-button');
    const progressContainer = document.getElementById('upload-progress-container');
    const progressBar = document.getElementById('upload-progress-bar');

    if (fileInput && fileNameDisplay) {
        fileInput.addEventListener('change', function() {
            if (fileInput.files.length > 0) {
                fileNameDisplay.textContent = fileInput.files[0].name;
                fileNameDisplay.classList.remove('text-gray-500');
                fileNameDisplay.classList.add('text-indigo-600');
            } else {
                fileNameDisplay.textContent = 'No file selected';
                fileNameDisplay.classList.remove('text-indigo-600');
                fileNameDisplay.classList.add('text-gray-500');
            }
        });
    }

    if (fileForm && uploadButton && progressContainer && progressBar) {
        fileForm.addEventListener('submit', function(e) {
            // Only handle the form if there's an actual file to upload
            if (fileInput && fileInput.files.length > 0) {
                e.preventDefault();

                uploadButton.disabled = true;
                uploadButton.classList.add('opacity-50', 'cursor-not-allowed');
                progressContainer.classList.remove('hidden');

                const formData = new FormData(fileForm);

                // Use fetch API to upload the file
                fetch('/api/v1/media/upload', {
                    method: 'POST',
                    body: formData
                })
                .then(response => {
                    if (!response.ok) {
                        throw new Error('Upload failed');
                    }
                    return response.json();
                })
                .then(data => {
                    console.log('Upload successful:', data);
                    window.location.href = '/dashboard';
                })
                .catch(error => {
                    console.error('Error:', error);
                    alert('Upload failed: ' + error.message);

                    uploadButton.disabled = false;
                    uploadButton.classList.remove('opacity-50', 'cursor-not-allowed');
                    progressContainer.classList.add('hidden');
                });

                // Simulate upload progress (since we don't have actual progress events)
                let progress = 0;
                const interval = setInterval(() => {
                    progress += 5;
                    if (progress >= 100) {
                        clearInterval(interval);
                    }
                    progressBar.style.width = `${progress}%`;
                }, 200);
            }
        });
    }
}

// Function to initialize tag input
function initializeTagInput() {
    const tagInput = document.getElementById('tag-input');
    const tagContainer = document.getElementById('tag-container');
    const hiddenTagInput = document.getElementById('tags');

    if (tagInput && tagContainer && hiddenTagInput) {
        // Initialize from existing value
        let tags = hiddenTagInput.value ? hiddenTagInput.value.split(',') : [];
        renderTags();

        tagInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault();

                const tag = tagInput.value.trim();
                if (tag && !tags.includes(tag)) {
                    tags.push(tag);
                    tagInput.value = '';
                    renderTags();
                }
            }
        });

        function renderTags() {
            // Update hidden input
            hiddenTagInput.value = tags.join(',');

            // Render tag elements
            tagContainer.innerHTML = '';
            tags.forEach(tag => {
                const tagElement = document.createElement('span');
                tagElement.classList.add('tag', 'mr-2', 'mb-2');

                const tagText = document.createElement('span');
                tagText.textContent = tag;

                const removeButton = document.createElement('button');
                removeButton.innerHTML = '&times;';
                removeButton.classList.add('ml-1', 'font-bold');
                removeButton.addEventListener('click', () => {
                    tags = tags.filter(t => t !== tag);
                    renderTags();
                });

                tagElement.appendChild(tagText);
                tagElement.appendChild(removeButton);
                tagContainer.appendChild(tagElement);
            });
        }
    }
}
