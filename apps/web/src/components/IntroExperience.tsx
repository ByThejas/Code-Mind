import { useCallback, useEffect, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Float, Html, RoundedBox } from "@react-three/drei";
import * as THREE from "three";
import "./IntroExperience.css";

interface EnterKeyProps {
  pressed: boolean;
  onPress: () => void;
}

function EnterKey({ pressed, onPress }: EnterKeyProps) {
  const groupRef = useRef<THREE.Group>(null);
  const topRef = useRef<THREE.Mesh>(null);
  const [hovered, setHovered] = useState(false);

  useFrame((state, delta) => {
    const group = groupRef.current;
    const top = topRef.current;

    if (!group || !top) return;

    const targetY = pressed ? -0.16 : hovered ? 0.06 : 0;

    group.position.y = THREE.MathUtils.damp(
      group.position.y,
      targetY,
      12,
      delta,
    );

    group.rotation.x = THREE.MathUtils.damp(
      group.rotation.x,
      hovered ? -0.035 : 0,
      5,
      delta,
    );

    group.rotation.y = THREE.MathUtils.damp(
      group.rotation.y,
      Math.sin(state.clock.elapsedTime * 0.3) * 0.025,
      3,
      delta,
    );

    const targetScale = hovered ? 1.025 : 1;

    group.scale.x = THREE.MathUtils.damp(
      group.scale.x,
      targetScale,
      8,
      delta,
    );

    group.scale.z = THREE.MathUtils.damp(
      group.scale.z,
      targetScale,
      8,
      delta,
    );

    const material = top.material as THREE.MeshPhysicalMaterial;

    material.roughness = THREE.MathUtils.damp(
      material.roughness,
      hovered ? 0.07 : 0.12,
      5,
      delta,
    );

    material.clearcoatRoughness = THREE.MathUtils.damp(
      material.clearcoatRoughness,
      hovered ? 0.025 : 0.06,
      5,
      delta,
    );
  });

  return (
    <Float
      speed={0.8}
      rotationIntensity={0.025}
      floatIntensity={0.09}
      floatingRange={[-0.025, 0.025]}
    >
      <group
        ref={groupRef}
        position={[0, -1.05, 0]}
        onClick={(event) => {
          event.stopPropagation();
          onPress();
        }}
        onPointerOver={(event) => {
          event.stopPropagation();
          setHovered(true);
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          setHovered(false);
          document.body.style.cursor = "default";
        }}
      >
        {/* Dark metallic foundation */}
        <RoundedBox
          args={[4.2, 1.52, 0.48]}
          radius={0.2}
          smoothness={10}
          castShadow
          receiveShadow
        >
          <meshPhysicalMaterial
            color="#454d5b"
            metalness={1}
            roughness={0.19}
            clearcoat={1}
            clearcoatRoughness={0.045}
          />
        </RoundedBox>

        {/* Bright chrome perimeter */}
        <RoundedBox
          args={[4.12, 1.44, 0.12]}
          radius={0.18}
          smoothness={10}
          position={[0, 0.025, 0.235]}
          castShadow
        >
          <meshPhysicalMaterial
            color="#dbe5f5"
            metalness={0.98}
            roughness={0.13}
            clearcoat={1}
            clearcoatRoughness={0.025}
          />
        </RoundedBox>

        {/* Glass-like upper surface */}
        <RoundedBox
          ref={topRef}
          args={[4.02, 1.34, 0.075]}
          radius={0.155}
          smoothness={10}
          position={[0, 0.035, 0.315]}
          castShadow
        >
          <meshPhysicalMaterial
            color="#c9d7eb"
            metalness={0.48}
            roughness={0.12}
            transparent
            opacity={0.82}
            transmission={0.12}
            thickness={0.18}
            ior={1.42}
            clearcoat={1}
            clearcoatRoughness={0.025}
            emissive="#9cbcf2"
            emissiveIntensity={0.035}
            side={THREE.DoubleSide}
          />
        </RoundedBox>

        {/* Thin inner glass highlight */}
        <RoundedBox
          args={[3.91, 1.23, 0.014]}
          radius={0.13}
          smoothness={10}
          position={[0, 0.04, 0.36]}
        >
          <meshBasicMaterial
            color="#ffffff"
            transparent
            opacity={hovered ? 0.48 : 0.24}
          />
        </RoundedBox>

        {/* Chrome key legend */}
        <mesh position={[0, -0.02, 0.375]}>
          <planeGeometry args={[1.45, 0.4]} />
          <meshBasicMaterial
            color="#111722"
            transparent
            opacity={0.94}
          />
        </mesh>

        {/* Actual text rendered in the 3D scene */}
        <mesh position={[0, -0.02, 0.382]}>
          <planeGeometry args={[1.36, 0.32]} />
          <meshBasicMaterial
            color="#f3f7ff"
            transparent
            opacity={0.98}
          />
        </mesh>

        {/* Use 3D text-free CSS label attached to the key */}
        <group position={[0, -0.02, 0.405]}>
          <mesh>
            <planeGeometry args={[1.1, 0.3]} />
            <meshBasicMaterial
              color="#252d3a"
              transparent
              opacity={0.96}
            />
          </mesh>
        </group>

        {/* Polished front-edge reflection */}
        <mesh position={[0, -0.69, 0.27]}>
          <boxGeometry args={[3.35, 0.012, 0.012]} />
          <meshBasicMaterial
            color="#ffffff"
            transparent
            opacity={hovered ? 0.95 : 0.42}
          />
        </mesh>

        {/* Keycap lettering */}
        <group position={[0, 0, 0.43]}>
          <Html
            center
            transform
            distanceFactor={2.6}
            style={{
              pointerEvents: "none",
              userSelect: "none",
              whiteSpace: "nowrap",
            }}
          >
            <div className="chrome-key-label">
              <span className="chrome-key-symbol">↵</span>
              <span>ENTER</span>
            </div>
          </Html>
        </group>

        {/* Soft light pool beneath the key */}
        <mesh
          position={[0, -0.88, -0.08]}
          rotation={[-Math.PI / 2, 0, 0]}
        >
          <circleGeometry args={[2.1, 64]} />
          <meshBasicMaterial
            color="#8eafff"
            transparent
            opacity={hovered ? 0.11 : 0.045}
            depthWrite={false}
          />
        </mesh>
      </group>
    </Float>
  );
}

interface IntroSceneProps {
  pressed: boolean;
  onPress: () => void;
}

function IntroScene({ pressed, onPress }: IntroSceneProps) {
  const movingLightRef = useRef<THREE.PointLight>(null);

  useFrame((state, delta) => {
    const light = movingLightRef.current;

    if (!light) return;

    light.position.x = THREE.MathUtils.damp(
      light.position.x,
      Math.sin(state.clock.elapsedTime * 0.32) * 3.5,
      1.5,
      delta,
    );

    light.position.z = 2.5 + Math.cos(state.clock.elapsedTime * 0.25);
  });

  return (
    <>
      {/* The same dark environment fills the complete viewport */}
      <color attach="background" args={["#050608"]} />

      <fog attach="fog" args={["#050608", 12, 35]} />

      <ambientLight intensity={0.7} />

      <hemisphereLight args={["#dce8ff", "#080a11", 1.1]} />

      {/* Large soft reflections across the chrome */}
      <directionalLight
        position={[-5, 6, 7]}
        intensity={4.5}
        color="#f4f7ff"
      />

      <directionalLight
        position={[5, 3, -4]}
        intensity={5}
        color="#718dc6"
      />

      <directionalLight
        position={[0, -1, 5]}
        intensity={2}
        color="#dce7ff"
      />

      <spotLight
        position={[-1, 7, 4]}
        angle={0.65}
        penumbra={1}
        intensity={65}
        color="#ffffff"
      />

      <pointLight
        ref={movingLightRef}
        position={[-3, 1, 2.5]}
        intensity={7}
        color="#b9d0ff"
        distance={16}
      />

      <pointLight
        position={[4, -1, 2]}
        intensity={5}
        color="#e5edff"
        distance={13}
      />

      <pointLight
        position={[0, -2, -2]}
        intensity={3}
        color="#4e70b6"
        distance={12}
      />

      {/* Full-width environment floor, without a canvas boundary */}
      <mesh
        rotation={[-Math.PI / 2, 0, 0]}
        position={[0, -3.15, -1]}
        receiveShadow
      >
        <planeGeometry args={[200, 200]} />
        <meshStandardMaterial
          color="#080a10"
          metalness={0.72}
          roughness={0.34}
        />
      </mesh>

      <EnterKey pressed={pressed} onPress={onPress} />
    </>
  );
}

interface IntroExperienceProps {
  onEnter: () => void;
}

export default function IntroExperience({
  onEnter,
}: IntroExperienceProps) {
  const [pressed, setPressed] = useState(false);
  const [transitioning, setTransitioning] = useState(false);
  const [status, setStatus] = useState("SYSTEM STANDBY");

  const onEnterRef = useRef(onEnter);
  const transitioningRef = useRef(false);
  const transitionTimerRef = useRef<number | null>(null);

  useEffect(() => {
    onEnterRef.current = onEnter;
  }, [onEnter]);

  const launch = useCallback(() => {
    if (transitioningRef.current) return;

    transitioningRef.current = true;

    setPressed(true);
    setTransitioning(true);
    setStatus("INITIALIZING CODEMIND...");

    transitionTimerRef.current = window.setTimeout(() => {
      onEnterRef.current();
    }, 900);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Enter" || event.repeat) return;

      event.preventDefault();
      launch();
    };

    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [launch]);

  useEffect(() => {
    return () => {
      if (transitionTimerRef.current !== null) {
        window.clearTimeout(transitionTimerRef.current);
      }

      document.body.style.cursor = "default";
    };
  }, []);

  return (
    <main
      className={`intro-experience ${
        transitioning ? "intro-transitioning" : ""
      }`}
    >
      {/* Full-screen 3D environment */}
      <div className="intro-world">
        <Canvas
          camera={{
            position: [0, 0.3, 8.8],
            fov: 39,
          }}
          dpr={[1, 1.75]}
          shadows
          gl={{
            antialias: true,
            alpha: false,
            powerPreference: "high-performance",
          }}
          onCreated={({ gl }) => {
            gl.setClearColor("#050608", 1);
            gl.toneMapping = THREE.ACESFilmicToneMapping;
            gl.toneMappingExposure = 1.35;
          }}
        >
          <IntroScene pressed={pressed} onPress={launch} />
        </Canvas>
      </div>

      {/* Atmosphere spans the whole page */}
      <div className="intro-vignette" />

      <div className="intro-grain" aria-hidden="true" />

      <header className="intro-topbar">
        <div className="intro-brand">
          <span className="brand-mark">C</span>
          <span>CODEMIND</span>
        </div>

        <div className="intro-system-status">
          <span className="status-indicator" />
          ENGINEERING INTELLIGENCE
        </div>
      </header>

      <section className="intro-content">
        <div className="intro-copy">
          <div className="intro-eyebrow">
            <span className="eyebrow-line" />
            YOUR REPOSITORY. UNDERSTOOD.
          </div>

          <h1 className="intro-heading">
            Think deeper.
            <br />
            <span>Build smarter.</span>
          </h1>

          <p className="intro-description">
            An engineering workspace designed to understand your code,
            trace dependencies, and help you build with confidence.
          </p>
        </div>

        {/* Intentionally no second ENTER button or key badge */}
        <div className="intro-key-caption">
          <span className="caption-line" />
          <span>INITIALIZE YOUR WORKSPACE</span>
          <span className="caption-line" />
        </div>

        <div className="intro-status-row">
          <span
            className={`intro-status-dot ${
              transitioning ? "status-active" : ""
            }`}
          />
          <span className="intro-status">{status}</span>
        </div>
      </section>

      <footer className="intro-footer">
        <span>REPOSITORY INTELLIGENCE SYSTEM</span>
        <span>BUILT FOR ENGINEERS</span>
      </footer>
    </main>
  );
}