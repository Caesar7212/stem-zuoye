const generateMindmapButton = document.getElementById('generate-mindmap');
generateMindmapButton.addEventListener('click', function () {
    const courseTitle = document.getElementById('course-title').value;
    const courseDescription = document.getElementById('course-description').value;
    const teachingObjectives = document.getElementById('teaching-objectives').value;

    const data = {
        course_title: courseTitle,
        course_description: courseDescription,
        teaching_objectives: teachingObjectives
    };

    fetch('/generate-mindmap', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    })
   .then(response => response.json())
   .then(mindmap => {
        // 处理返回的思维导图数据
        console.log(mindmap);
    })
   .catch(error => {
        console.error('Error:', error);
    });
});