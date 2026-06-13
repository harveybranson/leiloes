class WebGLHoverManager {
    constructor(container) {
        this.canvas = document.createElement('canvas');
        this.canvas.id = `hover-effect-canvas-${Date.now()}`;
        this.lastMouseMoveTime = performance.now();

        container.querySelector('.wrap-distort-canvas').appendChild(this.canvas);

        this.gl = this.canvas.getContext('webgl');
        if (!this.gl) {
            console.error('WebGL not supported');
            return;
        }

        this.images = [];
        this.textures = {};
        this.time = 0;

        this.initWebGL();
        if (!this.createShaders()) {
            console.error('Failed to create shaders');
            return;
        }
        this.createBuffers();
        this.startRender();

        window.addEventListener('resize', this.resize.bind(this));
        document.addEventListener('mousemove', this.onMouseMove.bind(this));
    }

    initWebGL() {
        this.resize();
    }

    resize() {
        const wrapper = this.canvas.parentElement;
        if (!wrapper) return;

        const rect = wrapper.getBoundingClientRect();

        this.canvas.width = rect.width * window.devicePixelRatio;
        this.canvas.height = rect.height * window.devicePixelRatio;

        this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    }

    createShaders() {
        const vertexSource = `
        attribute vec2 position;
        attribute vec2 aTexCoord;
        varying vec2 vTexCoord;
        void main() {
            vTexCoord = aTexCoord;
            gl_Position = vec4(position, 0.0, 1.0);
        }
		`;

        const fragmentSource = `
        precision highp float;
        varying vec2 vTexCoord;
        uniform sampler2D uTexture;
        uniform float uTime;
        uniform float uStrength;
        uniform vec2 uMouse;
        uniform bool uHover;

        void main() {
            vec2 uv = vTexCoord;

            if (uHover) {
                // Distortion logic adapted from the first snippet, without uScale:
                vec2 pos = uv;
                vec2 mouseDirection = pos - uMouse;
                float dist = length(mouseDirection);
                float angle = atan(mouseDirection.y, mouseDirection.x);

                // Wave distortion parameters:
                float wave = sin(dist * 15.0 - angle * 1.0) * 0.02;
                float effect = uStrength * smoothstep(0.45, 0.1, dist);

                vec2 offset = normalize(mouseDirection) * wave * effect;
                vec2 distortedPos = pos + offset;

                // Chromatic aberration effect:
                vec2 rgbDirection = normalize(mouseDirection);
                float rgbAmount = 0.02 * effect;

                float r = texture2D(uTexture, distortedPos + rgbDirection * rgbAmount).r;
                float g = texture2D(uTexture, distortedPos).g;
                float b = texture2D(uTexture, distortedPos - rgbDirection * rgbAmount).b;

                gl_FragColor = vec4(r, g, b, 1.0);
            } else {
                // If not hovering, just draw the image normally:
                gl_FragColor = texture2D(uTexture, uv);
            }
        }
		`;

        const vertexShader = this.compileShader(this.gl.VERTEX_SHADER, vertexSource);
        const fragmentShader = this.compileShader(this.gl.FRAGMENT_SHADER, fragmentSource);

        if (!vertexShader || !fragmentShader) return false;

        this.program = this.gl.createProgram();
        this.gl.attachShader(this.program, vertexShader);
        this.gl.attachShader(this.program, fragmentShader);
        this.gl.linkProgram(this.program);

        if (!this.gl.getProgramParameter(this.program, this.gl.LINK_STATUS)) {
            console.error('Program link error:', this.gl.getProgramInfoLog(this.program));
            return false;
        }

        this.gl.useProgram(this.program);

        this.attribLocations = {
            position: this.gl.getAttribLocation(this.program, 'position'),
            aTexCoord: this.gl.getAttribLocation(this.program, 'aTexCoord'),
        };

        this.uniformLocations = {
            uTexture: this.gl.getUniformLocation(this.program, 'uTexture'),
            uTime: this.gl.getUniformLocation(this.program, 'uTime'),
            uStrength: this.gl.getUniformLocation(this.program, 'uStrength'),
            uMouse: this.gl.getUniformLocation(this.program, 'uMouse'),
            uHover: this.gl.getUniformLocation(this.program, 'uHover'),
        };

        return true;
    }

    compileShader(type, source) {
        const shader = this.gl.createShader(type);
        this.gl.shaderSource(shader, source);
        this.gl.compileShader(shader);

        if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) {
            console.error('Shader compile error:', this.gl.getShaderInfoLog(shader));
            this.gl.deleteShader(shader);
            return null;
        }
        return shader;
    }

    createBuffers() {
        this.vertexBuffer = this.gl.createBuffer();

        this.texCoordBuffer = this.gl.createBuffer();
    }

    addImage(imgElement) {
        const imgSrc = imgElement.src;
        if (!this.textures[imgSrc]) {
            const image = new Image();
            image.crossOrigin = 'anonymous';
            image.src = imgSrc;
            image.onload = () => {
                const texture = this.createTexture(image);
                this.textures[imgSrc] = texture;
                this.addImageInfo(imgElement, texture);
            };
        } else {
            const texture = this.textures[imgSrc];
            this.addImageInfo(imgElement, texture);
        }
    }

    createTexture(image) {
        const texture = this.gl.createTexture();
        this.gl.bindTexture(this.gl.TEXTURE_2D, texture);

        this.gl.pixelStorei(this.gl.UNPACK_FLIP_Y_WEBGL, true);
        this.gl.texImage2D(
            this.gl.TEXTURE_2D, 0, this.gl.RGBA,
            this.gl.RGBA, this.gl.UNSIGNED_BYTE, image
        );
        this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_WRAP_S, this.gl.CLAMP_TO_EDGE);
        this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_WRAP_T, this.gl.CLAMP_TO_EDGE);
        this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_MIN_FILTER, this.gl.LINEAR);
        this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_MAG_FILTER, this.gl.LINEAR);

        return texture;
    }

    addImageInfo(imgElement, texture) {
        const imageInfo = {
            element: imgElement,
            texture: texture,
            strength: 0.0,
            targetStrength: 0.0,
            mouseX: 0.5,
            mouseY: 0.5,
        };

        this.images.push(imageInfo);
    }

    onMouseMove(e) {
        const clientX = e.clientX;
        const clientY = e.clientY;
        this.lastMouseMoveTime = performance.now();

        let hoveredImageFound = false;

        for (let img of this.images) {
            const rect = img.element.getBoundingClientRect();
            if (
                clientX >= rect.left &&
                clientX <= rect.right &&
                clientY >= rect.top &&
                clientY <= rect.bottom
            ) {
                img.targetStrength = 1.0;
                img.mouseX = (clientX - rect.left) / rect.width;
                img.mouseY = 1.0 - (clientY - rect.top) / rect.height;
                hoveredImageFound = true;
            } else {
                img.targetStrength = 0.0;
            }
        }
    }

    startRender() {
        this.lastTime = performance.now();
        this.render();
    }

    render = () => {
        if (!this.gl || !this.program) return;

        const currentTime = performance.now();
        const dt = Math.min((currentTime - this.lastTime) / 1000, 1 / 30);
        this.lastTime = currentTime;
        this.time += dt;

        const timeSinceLastMove = currentTime - this.lastMouseMoveTime;

        this.gl.clearColor(0, 0, 0, 0);
        this.gl.clear(this.gl.COLOR_BUFFER_BIT);

        this.gl.useProgram(this.program);

        for (let img of this.images) {
            if (img.targetStrength > 0 && timeSinceLastMove > 100) {
                img.targetStrength = 0.0;
            }

            img.strength += (img.targetStrength - img.strength) * dt * 4.0;
            img.strength = Math.max(0, Math.min(1, img.strength));

            const canvasRect = this.canvas.getBoundingClientRect();
            const rect = img.element.getBoundingClientRect();

            const x = (rect.left - canvasRect.left) / canvasRect.width * 2 - 1;
            const y = -((rect.top - canvasRect.top) / canvasRect.height * 2 - 1);
            const width = (rect.width / canvasRect.width) * 2;
            const height = (rect.height / canvasRect.height) * 2;

            const imageAspect = img.element.naturalWidth / img.element.naturalHeight;
            const canvasAspect = canvasRect.width / canvasRect.height;

            let scaleX = 1.0,
                scaleY = 1.0;

            if (imageAspect > canvasAspect) {
                scaleX = imageAspect / canvasAspect;
            } else {
                scaleY = canvasAspect / imageAspect;
            }

            const x1 = -scaleX,
                x2 = scaleX;
            const y1 = -scaleY,
                y2 = scaleY;

            const vertices = new Float32Array([
                x1, y1,
                x2, y1,
                x1, y2,
                x2, y2
            ]);

            const texCoords = new Float32Array([
                0, 0,
                1, 0,
                0, 1,
                1, 1
            ]);

            this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.vertexBuffer);
            this.gl.bufferData(this.gl.ARRAY_BUFFER, vertices, this.gl.STATIC_DRAW);
            this.gl.enableVertexAttribArray(this.attribLocations.position);
            this.gl.vertexAttribPointer(this.attribLocations.position, 2, this.gl.FLOAT, false, 0, 0);

            this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.texCoordBuffer);
            this.gl.bufferData(this.gl.ARRAY_BUFFER, texCoords, this.gl.STATIC_DRAW);
            this.gl.enableVertexAttribArray(this.attribLocations.aTexCoord);
            this.gl.vertexAttribPointer(this.attribLocations.aTexCoord, 2, this.gl.FLOAT, false, 0, 0);



            this.gl.activeTexture(this.gl.TEXTURE0);
            this.gl.bindTexture(this.gl.TEXTURE_2D, img.texture);
            this.gl.uniform1i(this.uniformLocations.uTexture, 0);

            this.gl.uniform1f(this.uniformLocations.uTime, this.time);
            this.gl.uniform1f(this.uniformLocations.uStrength, img.strength);

            if (img.strength > 0.0) {
                this.gl.uniform2f(this.uniformLocations.uMouse, img.mouseX, img.mouseY);
                this.gl.uniform1i(this.uniformLocations.uHover, 1);
            } else {
                this.gl.uniform2f(this.uniformLocations.uMouse, 0.5, 0.5);
                this.gl.uniform1i(this.uniformLocations.uHover, 0);
            }

            this.gl.drawArrays(this.gl.TRIANGLE_STRIP, 0, 4);
        }

        requestAnimationFrame(this.render);
    }
}

function initializeWebGLEffect(container, webGLHoverManager) {
    const imgElements = container.querySelectorAll('.wrap-img-distortion img');

    imgElements.forEach(img => {
        if (img.getAttribute('crossorigin') !== 'anonymous') {
            img.setAttribute('crossorigin', 'anonymous');
            const src = img.getAttribute('src');
            img.setAttribute('src', '');
            img.setAttribute('src', src);
        }

        if (!img.complete || !img.naturalWidth) {
            img.onload = () => {
                webGLHoverManager.addImage(img);
            };
        } else {
            webGLHoverManager.addImage(img);
        }
    });
}

function initWebGLEffect() {
	const distortion_items = document.querySelectorAll('.wrap-img-distortion')
	if(distortion_items.length && jQuery(window).width() >= 1024) {
		imagesLoaded(distortion_items, () => {
            setTimeout(() => {
             jQuery('.wrap-img-distortion').each(function() {
                const _this = jQuery(this)[0]
                const webGLHoverManager = new WebGLHoverManager(_this)
                _this.webGLHoverManager = webGLHoverManager
                initializeWebGLEffect(_this, webGLHoverManager)
            })
            }, 100)
		})
	}
}

initWebGLEffect()

// barba.hooks.after(data => {
// 	initWebGLEffect()
// })