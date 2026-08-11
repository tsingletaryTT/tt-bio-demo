"""GLSL sources for the two draw modes.

OpenGL 3.3 core: the QB2 reports 4.6 but 3.3 keeps the renderer usable on
developer laptops without changing anything.
"""

POINT_VERT = """
#version 330 core
layout(location = 0) in vec3 in_position;

uniform mat4 u_mvp;
uniform float u_point_size;

void main() {
    gl_Position = u_mvp * vec4(in_position, 1.0);
    // Shrink distant points so depth reads correctly without depth sorting.
    gl_PointSize = u_point_size / max(gl_Position.w, 0.1);
}
"""

POINT_FRAG = """
#version 330 core
out vec4 frag_color;

uniform vec3 u_color;
uniform float u_opacity;

void main() {
    // Carve a soft disc out of the square point sprite.
    vec2 offset = gl_PointCoord - vec2(0.5);
    float r = length(offset);
    if (r > 0.5) discard;
    float edge = smoothstep(0.5, 0.35, r);
    frag_color = vec4(u_color, u_opacity * edge);
}
"""

RIBBON_VERT = """
#version 330 core
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
"""

RIBBON_FRAG = """
#version 330 core
in vec3 v_normal;
in vec3 v_color;
out vec4 frag_color;

uniform float u_opacity;

void main() {
    vec3 n = normalize(v_normal);
    vec3 light = normalize(vec3(0.4, 0.8, 0.6));
    float diffuse = max(dot(n, light), 0.0);
    // Rim light keeps the silhouette readable against the dark background.
    float rim = pow(1.0 - abs(n.z), 2.0) * 0.35;
    vec3 shaded = v_color * (0.35 + 0.65 * diffuse) + vec3(rim) * 0.6;
    frag_color = vec4(shaded, u_opacity);
}
"""
