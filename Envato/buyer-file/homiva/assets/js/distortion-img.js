import {
    Renderer,
    Program,
    Mesh,
    Vec2,
    Texture,
    Flowmap,
    Plane
} from "./distortion-img-depend.js";

(function ($) {
    "use strict";

    const vertex = `
        attribute vec2 uv;
        attribute vec2 position;
        varying vec2 vUv;
        void main() {
            vUv = uv;
            gl_Position = vec4(position, 0.0, 0.45);
        }
    `;

    const fragment = `
        precision highp float;
        uniform sampler2D tImage;
        uniform sampler2D tFlow;
        varying vec2 vUv;

        void main() {
            vec3 flow = texture2D(tFlow, vUv).rgb;
            vec2 uv = vUv;
            uv += (flow.rg * flow.b * 120.0);
            vec3 tex = texture2D(tImage, uv).rgb;
            gl_FragColor = vec4(tex, 1.0);
        }
    `;

    /* 🔥 MAIN FUNCTION */
    function initDistortion(background) {

        const imageSrc = background.getAttribute("data-background");

        const renderer = new Renderer();
        const gl = renderer.gl;
        background.appendChild(gl.canvas);

        const mouse = new Vec2(0.5, 0.5);
        const lastMouse = new Vec2(0.5, 0.5);
        const velocity = new Vec2();
        let aspect = 1;

        function resize() {
            const rect = background.getBoundingClientRect();
            aspect = rect.width / rect.height;
            renderer.setSize(rect.width, rect.height);
        }
        window.addEventListener("resize", resize);
        resize();

        const flowmap = new Flowmap(gl, {
            falloff: 0.75,
            dissipation: 0.9,
            size: 1000
        });

        const geometry = new Plane(gl);

        const texture = new Texture(gl);
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => texture.image = img;
        img.src = imageSrc;

        const program = new Program(gl, {
            vertex,
            fragment,
            uniforms: {
                tImage: { value: texture },
                tFlow: flowmap.uniform
            }
        });

        const mesh = new Mesh(gl, { geometry, program });

        function updateMouse(e) {
            const rect = background.getBoundingClientRect();
            mouse.set(
                (e.clientX - rect.left) / rect.width,
                1 - (e.clientY - rect.top) / rect.height
            );
        }

        background.addEventListener("mousemove", updateMouse);

        const spring = 0.08;
        const friction = 0.7;
        const springVel = new Vec2();

        function update() {
            springVel.copy(mouse).sub(lastMouse).multiply(spring);
            velocity.add(springVel).multiply(friction);
            lastMouse.add(velocity);

            flowmap.mouse.copy(lastMouse);
            flowmap.velocity.copy(velocity);
            flowmap.aspect = aspect;
            flowmap.update();

            renderer.render({ scene: mesh });
            requestAnimationFrame(update);
        }

        requestAnimationFrame(update);
    }

    document.querySelectorAll(".tp-image-distortion")
        .forEach(initDistortion);

})(jQuery);
