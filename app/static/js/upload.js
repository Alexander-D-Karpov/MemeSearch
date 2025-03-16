// JavaScript for the media upload functionality

document.addEventListener('DOMContentLoaded', function() {
    const dropArea = document.getElementById('drop-area');
    const fileInput = document.getElementById('file-upload');
    const filePreview = document.getElementById('file-preview');
    const fileNameDisplay = document.getElementById('file-name');
    const uploadForm = document.getElementById('upload-form');
    const uploadButton = document.getElementById('upload-button');
    const progressContainer = document.getElementById('upload-progress-container');
    const progressBar = document.getElementById('upload-progress-bar');
    const progressText = document.getElementById('upload-progress-text');

    if (!dropArea || !fileInput) return;

    // Prevent default drag behaviors
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropArea.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    // Highlight drop area when item is dragged over it
    ['dragenter', 'dragover'].forEach(eventName => {
        dropArea.addEventListener(eventName, highlight, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropArea.addEventListener(eventName, unhighlight, false);
    });

    function highlight() {
        dropArea.classList.add('border-indigo-500', 'bg-indigo-50');
    }

    function unhighlight() {
        dropArea.classList.remove('border-indigo-500', 'bg-indigo-50');
    }

    // Handle dropped files
    dropArea.addEventListener('drop', handleDrop, false);

    function handleDrop(e) {
        const dt = e.dataTransfer;
        const files = dt.files;
        fileInput.files = files;
        updateFilePreview(files[0]);
    }

    // Handle selected files from file input
    fileInput.addEventListener('change', function() {
        if (fileInput.files.length > 0) {
            updateFilePreview(fileInput.files[0]);
        }
    });

    function updateFilePreview(file) {
        if (!file) return;

        // Update file name display
        fileNameDisplay.textContent = file.name;
        fileNameDisplay.classList.remove('text-gray-400');
        fileNameDisplay.classList.add('text-indigo-600', 'font-medium');

        // Clear the preview
        filePreview.innerHTML = '';
        filePreview.classList.remove('hidden');

        // Check if file is an image or video
        if (file.type.startsWith('image/')) {
            const img = document.createElement('img');
            img.classList.add('max-h-48', 'max-w-full', 'rounded-lg', 'shadow');
            img.file = file;

            const reader = new FileReader();
            reader.onload = (function(aImg) {
                return function(e) {
                    aImg.src = e.target.result;
                };
            })(img);
            reader.readAsDataURL(file);

            filePreview.appendChild(img);
        } else if (file.type.startsWith('video/')) {
            const video = document.createElement('video');
            video.classList.add('max-h-48', 'max-w-full', 'rounded-lg', 'shadow');
            video.controls = true;

            const reader = new FileReader();
            reader.onload = (function(aVideo) {
                return function(e) {
                    aVideo.src = e.target.result;
                };
            })(video);
            reader.readAsDataURL(file);

            filePreview.appendChild(video);
        } else {
            // For other file types, show an icon
            const icon = document.createElement('div');
            icon.innerHTML = '<i class="fas fa-file text-6xl text-indigo-500"></i>';
            icon.classList.add('text-center', 'py-8');
            filePreview.appendChild(icon);
        }
    }

    // Handle form submission
    if (uploadForm) {
        uploadForm.addEventListener('submit', function(e) {
            if (fileInput.files.length === 0) {
                e.preventDefault();
                alert('Please select a file to upload');
                return;
            }

            // Start upload process
            e.preventDefault();
            uploadFile(fileInput.files[0]);
        });
    }

    function uploadFile(file) {
        // Disable upload button and show progress
        uploadButton.disabled = true;
        uploadButton.classList.add('opacity-50', 'cursor-not-allowed');
        progressContainer.classList.remove('hidden');
        progressText.textContent = 'Uploading...';

        // Create FormData
        const formData = new FormData(uploadForm);

        // Use fetch API to upload the file
        fetch('/api/v1/media/upload', {
            method: 'POST',
            body: formData
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.log('Upload successful:', data);
            progressText.textContent = 'Upload complete! Redirecting...';
            progressBar.style.width = '100%';

            // Redirect to dashboard after a short delay
            setTimeout(() => {
                window.location.href = '/dashboard';
            }, 1000);
        })
        .catch(error => {
            console.error('Error:', error);
            progressText.textContent = 'Upload failed! Please try again.';
            uploadButton.disabled = false;
            uploadButton.classList.remove('opacity-50', 'cursor-not-allowed');
        });

        // Simulate upload progress (since we don't have actual progress events)
        let progress = 0;
        const interval = setInterval(() => {
            progress += 5;
            if (progress >= 90) {
                clearInterval(interval);
            }
            progressBar.style.width = `${progress}%`;
        }, 300);
    }
});
