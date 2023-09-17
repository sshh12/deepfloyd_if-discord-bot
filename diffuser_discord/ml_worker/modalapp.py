import modal
import asyncio

CACHE_DIR = "/root/cache"


def download_models():
    from huggingface_hub import snapshot_download

    ignore = ["*.bin", "*.onnx_data", "*/diffusion_pytorch_model.safetensors"]
    snapshot_download("stabilityai/stable-diffusion-xl-base-1.0", ignore_patterns=ignore, cache_dir=CACHE_DIR)
    snapshot_download("stabilityai/stable-diffusion-xl-refiner-1.0", ignore_patterns=ignore, cache_dir=CACHE_DIR)


image = (
    modal.Image.debian_slim()
    .apt_install("libglib2.0-0", "libsm6", "libxrender1", "libxext6", "ffmpeg", "libgl1")
    .pip_install(
        "diffusers~=0.21",
        "invisible_watermark~=0.1",
        "transformers~=4.31",
        "accelerate~=0.21",
        "safetensors~=0.3",
    )
    .run_function(download_models)
)

stub = modal.Stub("diffuser-discord-bot", image=image)


@stub.cls(gpu=modal.gpu.A10G(), container_idle_timeout=60)
class Model:
    def __enter__(self):
        import torch
        from diffusers import DiffusionPipeline

        load_options = dict(
            torch_dtype=torch.float16, use_safetensors=True, variant="fp16", device_map="auto", cache_dir=CACHE_DIR
        )

        self.base = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0", **load_options)
        self.refiner = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-refiner-1.0",
            text_encoder_2=self.base.text_encoder_2,
            vae=self.base.vae,
            **load_options,
        )

    @modal.method()
    def inference(self, prompt, seed, steps, high_noise_frac, negative_prompt):
        import torch

        generator = torch.manual_seed(seed)
        image = self.base(
            prompt=prompt,
            negative_prompt=negative_prompt,
            num_inference_steps=steps,
            denoising_end=high_noise_frac,
            output_type="latent",
            generator=generator,
        ).images
        image = self.refiner(
            prompt=prompt,
            negative_prompt=negative_prompt,
            num_inference_steps=steps,
            denoising_start=high_noise_frac,
            image=image,
            generator=generator,
        ).images[0]

        import io

        byte_stream = io.BytesIO()
        image.save(byte_stream, format="PNG")
        image_bytes = byte_stream.getvalue()

        return image_bytes


@stub.function(
    allow_concurrent_inputs=20,
    mounts=[
        modal.Mount.from_local_python_packages(
            "diffuser_discord.ml_worker.imgur_utils", "diffuser_discord.ml_worker.image_utils"
        )
    ],
    secrets=[
        modal.Secret.from_name("imgur-secret"),
    ],
)
async def generate_images(prompts, seed=0, steps=30, high_noise_frac=0.8, negative_prompt="disfigured, ugly, deformed"):
    import io
    from PIL import Image
    from diffuser_discord.ml_worker import imgur_utils, image_utils

    modal_model = Model()

    images = []
    for partial_result in modal_model.inference.starmap(
        [(prompt, seed + i) for i, prompt in enumerate(prompts)],
        kwargs=dict(steps=steps, high_noise_frac=high_noise_frac, negative_prompt=negative_prompt),
    ):
        await asyncio.sleep(0.5)
        images.append(Image.open(io.BytesIO(partial_result)))

    img = image_utils.image_grid(images)
    return imgur_utils.upload_to_imgur(img)


@stub.local_entrypoint()
def main():
    upload_url = generate_images.remote(["a potato"])
    print(upload_url)
