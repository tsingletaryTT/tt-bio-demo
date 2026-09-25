"use strict";

// WebGL2 (GLSL ES 300) translations of ui/shaders.py's RIBBON_VERT/
// RIBBON_FRAG. Same uniforms, same lighting math -- see that file for the
// original desktop GLSL 330 core source this is ported from.

const RIBBON_VERT_SRC = `#version 300 es
layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec3 in_color;

uniform mat4 u_mvp;
uniform mat4 u_model;

out vec3 v_normal;
out vec3 v_color;

void main() {
    gl_Position = u_mvp * vec4(in_position, 1.0);
    v_normal = mat3(u_model) * in_normal;
    v_color = in_color;
}
`;

const RIBBON_FRAG_SRC = `#version 300 es
precision highp float;
in vec3 v_normal;
in vec3 v_color;
out vec4 frag_color;

uniform float u_opacity;

void main() {
    vec3 n = normalize(v_normal);
    vec3 light = normalize(vec3(0.4, 0.8, 0.6));
    float diffuse = max(dot(n, light), 0.0);
    float rim = pow(1.0 - abs(n.z), 2.0) * 0.35;
    vec3 shaded = v_color * (0.35 + 0.65 * diffuse) + vec3(rim) * 0.6;
    frag_color = vec4(shaded, u_opacity);
}
`;

if (typeof module !== "undefined") {
  module.exports = { RIBBON_VERT_SRC, RIBBON_FRAG_SRC };
}
if (typeof window !== "undefined") {
  window.RibbonShaders = { RIBBON_VERT_SRC, RIBBON_FRAG_SRC };
}
