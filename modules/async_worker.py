import threading
from modules.patch import PatchSettings, patch_settings, patch_all

patch_all()


class AsyncTask:
    def __init__(self, args):
        from modules.flags import Performance, MetadataScheme, ip_list, controlnet_image_count
        from modules.util import get_enabled_loras
        from modules.config import default_max_lora_number
        import args_manager

        self.args = args.copy()
        self.yields = []
        self.results = []
        self.last_stop = False
        self.processing = False

        self.performance_loras = []

        if len(args) == 0:
            return

        args.reverse()
        self.generate_image_grid = args.pop()
        self.prompt = args.pop()
        self.negative_prompt = args.pop()
        self.translate_prompts = args.pop()
        self.style_selections = args.pop()

        self.performance_selection = Performance(args.pop())
        self.steps = self.performance_selection.steps()

        self.aspect_ratios_selection = args.pop()
        self.image_number = args.pop()
        self.output_format = args.pop()
        self.seed = int(args.pop())
        self.read_wildcards_in_order = args.pop()
        self.sharpness = args.pop()
        self.cfg_scale = args.pop()
        self.base_model_name = args.pop()
        self.refiner_model_name = args.pop()
        self.refiner_switch = args.pop()
        self.loras = get_enabled_loras([(bool(args.pop()), str(args.pop()), float(args.pop())) for _ in
                                        range(default_max_lora_number)])
        self.input_image_checkbox = args.pop()
        self.current_tab = args.pop()
        self.uov_method = args.pop()
        self.uov_input_image = args.pop()
        self.outpaint_selections = args.pop()
        self.inpaint_input_image = args.pop()
        self.inpaint_additional_prompt = args.pop()
        self.inpaint_mask_image_upload = args.pop()

        self.disable_preview = args.pop()
        self.disable_intermediate_results = args.pop()
        self.disable_seed_increment = args.pop()
        self.black_out_nsfw = args.pop()
        self.adm_scaler_positive = args.pop()
        self.adm_scaler_negative = args.pop()
        self.adm_scaler_end = args.pop()
        self.adaptive_cfg = args.pop()
        self.clip_skip = args.pop()
        self.sampler_name = args.pop()
        self.scheduler_name = args.pop()
        self.vae_name = args.pop()
        self.overwrite_step = args.pop()
        self.overwrite_switch = args.pop()
        self.overwrite_width = args.pop()
        self.overwrite_height = args.pop()
        self.overwrite_vary_strength = args.pop()
        self.overwrite_upscale_strength = args.pop()
        self.mixing_image_prompt_and_vary_upscale = args.pop()
        self.mixing_image_prompt_and_inpaint = args.pop()
        self.debugging_cn_preprocessor = args.pop()
        self.skipping_cn_preprocessor = args.pop()
        self.canny_low_threshold = args.pop()
        self.canny_high_threshold = args.pop()
        self.refiner_swap_method = args.pop()
        self.controlnet_softness = args.pop()
        self.freeu_enabled = args.pop()
        self.freeu_b1 = args.pop()
        self.freeu_b2 = args.pop()
        self.freeu_s1 = args.pop()
        self.freeu_s2 = args.pop()
        self.debugging_inpaint_preprocessor = args.pop()
        self.inpaint_disable_initial_latent = args.pop()
        self.inpaint_engine = args.pop()
        self.inpaint_strength = args.pop()
        self.inpaint_respective_field = args.pop()
        self.inpaint_mask_upload_checkbox = args.pop()
        self.invert_mask_checkbox = args.pop()
        self.inpaint_erode_or_dilate = args.pop()
        self.save_metadata_to_images = args.pop() if not args_manager.args.disable_metadata else False
        self.metadata_scheme = MetadataScheme(
            args.pop()) if not args_manager.args.disable_metadata else MetadataScheme.FOOOCUS

        self.cn_tasks = {x: [] for x in ip_list}
        for _ in range(controlnet_image_count):
            cn_img = args.pop()
            cn_stop = args.pop()
            cn_weight = args.pop()
            cn_type = args.pop()
            if cn_img is not None:
                self.cn_tasks[cn_type].append([cn_img, cn_stop, cn_weight])


async_tasks = []


class EarlyReturnException:
    pass


def worker():
    global async_tasks

    import os
    import traceback
    import math
    import numpy as np
    import torch
    import time
    import shared
    import random
    import copy
    import cv2
    import modules.default_pipeline as pipeline
    import modules.core as core
    import modules.flags as flags
    import modules.config
    import modules.patch
    import ldm_patched.modules.model_management
    import extras.preprocessors as preprocessors
    import modules.inpaint_worker as inpaint_worker
    import modules.constants as constants
    import extras.ip_adapter as ip_adapter
    import extras.face_crop
    import fooocus_version

    from extras.censor import default_censor
    from modules.sdxl_styles import apply_style, get_random_style, fooocus_expansion, apply_arrays, random_style_name
    from modules.private_logger import log
    from extras.expansion import safe_str
    from modules.util import (remove_empty_str, HWC3, resize_image, get_image_shape_ceil, set_image_shape_ceil,
                              get_shape_ceil, resample_image, erode_or_dilate, parse_lora_references_from_prompt,
                              apply_wildcards)
    from modules.upscaler import perform_upscale
    from modules.flags import Performance
    from modules.meta_parser import get_metadata_parser

    pid = os.getpid()
    print(f'Started worker with PID {pid}')

    try:
        async_gradio_app = shared.gradio_root
        flag = f'''App started successful. Use the app with {str(async_gradio_app.local_url)} or {str(async_gradio_app.server_name)}:{str(async_gradio_app.server_port)}'''
        if async_gradio_app.share:
            flag += f''' or {async_gradio_app.share_url}'''
        print(flag)
    except Exception as e:
        print(e)

    def progressbar(async_task, number, text):
        print(f'[Fooocus] {text}')
        async_task.yields.append(['preview', (number, text, None)])

    def yield_result(async_task, imgs, black_out_nsfw, censor=True, do_not_show_finished_images=False,
                     progressbar_index=flags.preparation_step_count):
        if not isinstance(imgs, list):
            imgs = [imgs]

        if censor and (modules.config.default_black_out_nsfw or black_out_nsfw):
            progressbar(async_task, progressbar_index, 'Checking for NSFW content ...')
            imgs = default_censor(imgs)

        async_task.results = async_task.results + imgs

        if do_not_show_finished_images:
            return

        async_task.yields.append(['results', async_task.results])
        return

    def build_image_wall(async_task):
        results = []

        if len(async_task.results) < 2:
            return

        for img in async_task.results:
            if isinstance(img, str) and os.path.exists(img):
                img = cv2.imread(img)
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            if not isinstance(img, np.ndarray):
                return
            if img.ndim != 3:
                return
            results.append(img)

        H, W, C = results[0].shape

        for img in results:
            Hn, Wn, Cn = img.shape
            if H != Hn:
                return
            if W != Wn:
                return
            if C != Cn:
                return

        cols = float(len(results)) ** 0.5
        cols = int(math.ceil(cols))
        rows = float(len(results)) / float(cols)
        rows = int(math.ceil(rows))

        wall = np.zeros(shape=(H * rows, W * cols, C), dtype=np.uint8)

        for y in range(rows):
            for x in range(cols):
                if y * cols + x < len(results):
                    img = results[y * cols + x]
                    wall[y * H:y * H + H, x * W:x * W + W, :] = img

        # must use deep copy otherwise gradio is super laggy. Do not use list.append() .
        async_task.results = async_task.results + [wall]
        return

    @torch.no_grad()
    @torch.inference_mode()
    def handler(async_task: AsyncTask):
        preparation_start_time = time.perf_counter()
        async_task.processing = True

        async_task.outpaint_selections = [o.lower() for o in async_task.outpaint_selections]
        base_model_additional_loras = []
        async_task.uov_method = async_task.uov_method.lower()

        if fooocus_expansion in async_task.style_selections:
            use_expansion = True
            async_task.style_selections.remove(fooocus_expansion)
        else:
            use_expansion = False

        use_style = len(async_task.style_selections) > 0

        if async_task.base_model_name == async_task.refiner_model_name:
            print(f'Refiner disabled because base model and refiner are same.')
            async_task.refiner_model_name = 'None'

        if async_task.performance_selection == Performance.EXTREME_SPEED:
            set_lcm_defaults(async_task)
        elif async_task.performance_selection == Performance.LIGHTNING:
            set_lightning_defaults(async_task)
        elif async_task.performance_selection == Performance.HYPER_SD:
            set_hyper_sd_defaults(async_task)

        if async_task.translate_prompts:
            translate_prompts(async_task)

        print(f'[Parameters] Adaptive CFG = {async_task.adaptive_cfg}')
        print(f'[Parameters] CLIP Skip = {async_task.clip_skip}')
        print(f'[Parameters] Sharpness = {async_task.sharpness}')
        print(f'[Parameters] ControlNet Softness = {async_task.controlnet_softness}')
        print(f'[Parameters] ADM Scale = '
              f'{async_task.adm_scaler_positive} : '
              f'{async_task.adm_scaler_negative} : '
              f'{async_task.adm_scaler_end}')
        print(f'[Parameters] Seed = {async_task.seed}')

        apply_patch_settings(async_task)

        print(f'[Parameters] CFG = {async_task.cfg_scale}')

        initial_latent = None
        denoising_strength = 1.0
        tiled = False

        width, height = async_task.aspect_ratios_selection.replace('×', ' ').split(' ')[:2]
        width, height = int(width), int(height)

        skip_prompt_processing = False

        inpaint_parameterized = async_task.inpaint_engine != 'None'
        inpaint_image = None
        inpaint_mask = None
        inpaint_head_model_path = None

        use_synthetic_refiner = False

        controlnet_canny_path = None
        controlnet_cpds_path = None
        clip_vision_path, ip_negative_path, ip_adapter_path, ip_adapter_face_path = None, None, None, None

        goals = []
        tasks = []

        if async_task.input_image_checkbox:
            if (async_task.current_tab == 'uov' or (
                    async_task.current_tab == 'ip' and async_task.mixing_image_prompt_and_vary_upscale)) \
                    and async_task.uov_method != flags.disabled and async_task.uov_input_image is not None:
                async_task.uov_input_image = HWC3(async_task.uov_input_image)
                if 'vary' in async_task.uov_method:
                    goals.append('vary')
                elif 'upscale' in async_task.uov_method:
                    goals.append('upscale')
                    if 'fast' in async_task.uov_method:
                        skip_prompt_processing = True
                    else:
                        async_task.steps = async_task.performance_selection.steps_uov()

                    progressbar(async_task, 1, 'Downloading upscale models ...')
                    modules.config.downloading_upscale_model()
            if (async_task.current_tab == 'inpaint' or (
                    async_task.current_tab == 'ip' and async_task.mixing_image_prompt_and_inpaint)) \
                    and isinstance(async_task.inpaint_input_image, dict):
                inpaint_image = async_task.inpaint_input_image['image']
                inpaint_mask = async_task.inpaint_input_image['mask'][:, :, 0]

                if async_task.inpaint_mask_upload_checkbox:
                    if isinstance(async_task.inpaint_mask_image_upload, dict):
                        if (isinstance(async_task.inpaint_mask_image_upload['image'], np.ndarray)
                                and isinstance(async_task.inpaint_mask_image_upload['mask'], np.ndarray)
                                and async_task.inpaint_mask_image_upload['image'].ndim == 3):
                            async_task.inpaint_mask_image_upload = np.maximum(async_task.inpaint_mask_image_upload['image'], async_task.inpaint_mask_image_upload['mask'])
                    if isinstance(async_task.inpaint_mask_image_upload, np.ndarray) and async_task.inpaint_mask_image_upload.ndim == 3:
                        H, W, C = inpaint_image.shape
                        async_task.inpaint_mask_image_upload = resample_image(async_task.inpaint_mask_image_upload, width=W, height=H)
                        async_task.inpaint_mask_image_upload = np.mean(async_task.inpaint_mask_image_upload, axis=2)
                        async_task.inpaint_mask_image_upload = (async_task.inpaint_mask_image_upload > 127).astype(np.uint8) * 255
                        async_task.inpaint_mask = np.maximum(inpaint_mask, async_task.inpaint_mask_image_upload)

                if int(async_task.inpaint_erode_or_dilate) != 0:
                    async_task.inpaint_mask = erode_or_dilate(async_task.inpaint_mask, async_task.inpaint_erode_or_dilate)

                if async_task.invert_mask_checkbox:
                    async_task.inpaint_mask = 255 - async_task.inpaint_mask

                inpaint_image = HWC3(inpaint_image)
                if isinstance(inpaint_image, np.ndarray) and isinstance(inpaint_mask, np.ndarray) \
                        and (np.any(inpaint_mask > 127) or len(async_task.outpaint_selections) > 0):
                    progressbar(async_task, 1, 'Downloading upscale models ...')
                    modules.config.downloading_upscale_model()
                    if inpaint_parameterized:
                        progressbar(async_task, 1, 'Downloading inpainter ...')
                        inpaint_head_model_path, inpaint_patch_model_path = modules.config.downloading_inpaint_models(
                            async_task.inpaint_engine)
                        base_model_additional_loras += [(inpaint_patch_model_path, 1.0)]
                        print(f'[Inpaint] Current inpaint model is {inpaint_patch_model_path}')
                        if async_task.refiner_model_name == 'None':
                            use_synthetic_refiner = True
                            async_task.refiner_switch = 0.8
                    else:
                        inpaint_head_model_path, inpaint_patch_model_path = None, None
                        print(f'[Inpaint] Parameterized inpaint is disabled.')
                    if async_task.inpaint_additional_prompt != '':
                        if async_task.prompt == '':
                            async_task.prompt = async_task.inpaint_additional_prompt
                        else:
                            async_task.prompt = async_task.inpaint_additional_prompt + '\n' + async_task.prompt
                    goals.append('inpaint')
            if async_task.current_tab == 'ip' or \
                    async_task.mixing_image_prompt_and_vary_upscale or \
                    async_task.mixing_image_prompt_and_inpaint:
                goals.append('cn')
                progressbar(async_task, 1, 'Downloading control models ...')
                if len(async_task.cn_tasks[flags.cn_canny]) > 0:
                    controlnet_canny_path = modules.config.downloading_controlnet_canny()
                if len(async_task.cn_tasks[flags.cn_cpds]) > 0:
                    controlnet_cpds_path = modules.config.downloading_controlnet_cpds()
                if len(async_task.cn_tasks[flags.cn_ip]) > 0:
                    clip_vision_path, ip_negative_path, ip_adapter_path = modules.config.downloading_ip_adapters('ip')
                if len(async_task.cn_tasks[flags.cn_ip_face]) > 0:
                    clip_vision_path, ip_negative_path, ip_adapter_face_path = modules.config.downloading_ip_adapters(
                        'face')


        # Load or unload CNs
        progressbar(async_task, 1, 'Loading control models ...')
        pipeline.refresh_controlnets([controlnet_canny_path, controlnet_cpds_path])
        ip_adapter.load_ip_adapter(clip_vision_path, ip_negative_path, ip_adapter_path)
        ip_adapter.load_ip_adapter(clip_vision_path, ip_negative_path, ip_adapter_face_path)

        height, switch, width = apply_overrides(async_task, height, width)

        print(f'[Parameters] Sampler = {async_task.sampler_name} - {async_task.scheduler_name}')
        print(f'[Parameters] Steps = {async_task.steps} - {switch}')

        progressbar(async_task, 1, 'Initializing ...')

        tasks = []
        if not skip_prompt_processing:
            tasks, use_expansion = process_prompt(async_task, base_model_additional_loras, use_expansion, use_style,
                                                  use_synthetic_refiner)

        if len(goals) > 0:
            progressbar(async_task, 7, 'Image processing ...')

        if 'vary' in goals:
            height, initial_latent, width = apply_vary(async_task, denoising_strength, switch)

        if 'upscale' in goals:
            try:
                denoising_strength, height, initial_latent, tiled, width = apply_upscale(async_task, switch)
            except EarlyReturnException:
                return
        if 'inpaint' in goals:
            try:
                denoising_strength, initial_latent, height, width = apply_inpaint(async_task, initial_latent,
                                                                                  inpaint_head_model_path, inpaint_image,
                                                                                  inpaint_mask, inpaint_parameterized,
                                                                                  switch)
            except EarlyReturnException:
                return

        if 'cn' in goals:
            apply_control_nets(async_task, height, ip_adapter_face_path, ip_adapter_path, width)
            if async_task.debugging_cn_preprocessor:
                return

        if async_task.freeu_enabled:
            apply_freeu(async_task)

        all_steps = async_task.steps * async_task.image_number

        print(f'[Parameters] Denoising Strength = {denoising_strength}')

        if isinstance(initial_latent, dict) and 'samples' in initial_latent:
            log_shape = initial_latent['samples'].shape
        else:
            log_shape = f'Image Space {(height, width)}'

        print(f'[Parameters] Initial Latent shape: {log_shape}')

        preparation_time = time.perf_counter() - preparation_start_time
        print(f'Preparation time: {preparation_time:.2f} seconds')

        final_scheduler_name = patch_samplers(async_task)
        print(f'Using {final_scheduler_name} scheduler.')

        async_task.yields.append(['preview', (flags.preparation_step_count, 'Moving model to GPU ...', None)])

        processing_start_time = time.perf_counter()

        def callback(step, x0, x, total_steps, y):
            done_steps = current_task_id * async_task.steps + step
            async_task.yields.append(['preview', (
                int(flags.preparation_step_count + (100 - flags.preparation_step_count) * float(done_steps) / float(all_steps)),
                f'Sampling step {step + 1}/{total_steps}, image {current_task_id + 1}/{async_task.image_number} ...', y)])

        for current_task_id, task in enumerate(tasks):
            current_progress = int(flags.preparation_step_count + (100 - flags.preparation_step_count) * float(
                current_task_id * async_task.steps) / float(all_steps))
            progressbar(async_task, current_progress,
                        f'Preparing task {current_task_id + 1}/{async_task.image_number} ...')
            execution_start_time = time.perf_counter()

            try:
                process_task(all_steps, async_task, callback, controlnet_canny_path, controlnet_cpds_path,
                             current_task_id, denoising_strength, final_scheduler_name, goals, initial_latent,
                             switch, task, tasks, tiled, use_expansion, width, height)
            except ldm_patched.modules.model_management.InterruptProcessingException:
                if async_task.last_stop == 'skip':
                    print('User skipped')
                    async_task.last_stop = False
                    continue
                else:
                    print('User stopped')
                    break

            execution_time = time.perf_counter() - execution_start_time
            print(f'Generating and saving time: {execution_time:.2f} seconds')

        async_task.processing = False

        processing_time = time.perf_counter() - processing_start_time
        print(f'Processing time (total): {processing_time:.2f} seconds')

    def process_task(all_steps, async_task, callback, controlnet_canny_path, controlnet_cpds_path, current_task_id,
                     denoising_strength, final_scheduler_name, goals, initial_latent, switch, task, tasks,
                     tiled, use_expansion, width, height):
        if async_task.last_stop is not False:
            ldm_patched.modules.model_management.interrupt_current_processing()
        positive_cond, negative_cond = task['c'], task['uc']
        if 'cn' in goals:
            for cn_flag, cn_path in [
                (flags.cn_canny, controlnet_canny_path),
                (flags.cn_cpds, controlnet_cpds_path)
            ]:
                for cn_img, cn_stop, cn_weight in async_task.cn_tasks[cn_flag]:
                    positive_cond, negative_cond = core.apply_controlnet(
                        positive_cond, negative_cond,
                        pipeline.loaded_ControlNets[cn_path], cn_img, cn_weight, 0, cn_stop)
        imgs = pipeline.process_diffusion(
            positive_cond=positive_cond,
            negative_cond=negative_cond,
            steps=async_task.steps,
            switch=switch,
            width=width,
            height=height,
            image_seed=task['task_seed'],
            callback=callback,
            sampler_name=async_task.sampler_name,
            scheduler_name=final_scheduler_name,
            latent=initial_latent,
            denoise=denoising_strength,
            tiled=tiled,
            cfg_scale=async_task.cfg_scale,
            refiner_swap_method=async_task.refiner_swap_method,
            disable_preview=async_task.disable_preview
        )
        del task['c'], task['uc'], positive_cond, negative_cond  # Save memory
        if inpaint_worker.current_task is not None:
            imgs = [inpaint_worker.current_task.post_process(x) for x in imgs]
        current_progress = int(flags.preparation_step_count + (100 - flags.preparation_step_count) * float(
            (current_task_id + 1) * async_task.steps) / float(all_steps))
        if modules.config.default_black_out_nsfw or async_task.black_out_nsfw:
            progressbar(async_task, current_progress, 'Checking for NSFW content ...')
            imgs = default_censor(imgs)
        progressbar(async_task, current_progress,
                    f'Saving image {current_task_id + 1}/{async_task.image_number} to system ...')
        img_paths = save_and_log(async_task, height, imgs, task, use_expansion, width)
        yield_result(async_task, img_paths, async_task.black_out_nsfw, False,
                     do_not_show_finished_images=len(tasks) == 1 or async_task.disable_intermediate_results)

        return imgs

    def apply_patch_settings(async_task):
        patch_settings[pid] = PatchSettings(
            async_task.sharpness,
            async_task.adm_scaler_end,
            async_task.adm_scaler_positive,
            async_task.adm_scaler_negative,
            async_task.controlnet_softness,
            async_task.adaptive_cfg
        )

    def save_and_log(async_task, height, imgs, task, use_expansion, width) -> list:
        img_paths = []
        for x in imgs:
            d = [('Prompt', 'prompt', task['log_positive_prompt']),
                 ('Negative Prompt', 'negative_prompt', task['log_negative_prompt']),
                 ('Fooocus V2 Expansion', 'prompt_expansion', task['expansion']),
                 ('Styles', 'styles',
                  str(task['styles'] if not use_expansion else [fooocus_expansion] + task['styles'])),
                 ('Performance', 'performance', async_task.performance_selection.value)]

            if async_task.performance_selection.steps() != async_task.steps:
                d.append(('Steps', 'steps', async_task.steps))

            d += [('Resolution', 'resolution', str((width, height))),
                  ('Guidance Scale', 'guidance_scale', async_task.cfg_scale),
                  ('Sharpness', 'sharpness', async_task.sharpness),
                  ('ADM Guidance', 'adm_guidance', str((
                      modules.patch.patch_settings[pid].positive_adm_scale,
                      modules.patch.patch_settings[pid].negative_adm_scale,
                      modules.patch.patch_settings[pid].adm_scaler_end))),
                  ('Base Model', 'base_model', async_task.base_model_name),
                  ('Refiner Model', 'refiner_model', async_task.refiner_model_name),
                  ('Refiner Switch', 'refiner_switch', async_task.refiner_switch)]

            if async_task.refiner_model_name != 'None':
                if async_task.overwrite_switch > 0:
                    d.append(('Overwrite Switch', 'overwrite_switch', async_task.overwrite_switch))
                if async_task.refiner_swap_method != flags.refiner_swap_method:
                    d.append(('Refiner Swap Method', 'refiner_swap_method', async_task.refiner_swap_method))
            if modules.patch.patch_settings[pid].adaptive_cfg != modules.config.default_cfg_tsnr:
                d.append(
                    ('CFG Mimicking from TSNR', 'adaptive_cfg', modules.patch.patch_settings[pid].adaptive_cfg))

            if async_task.clip_skip > 1:
                d.append(('CLIP Skip', 'clip_skip', async_task.clip_skip))
            d.append(('Sampler', 'sampler', async_task.sampler_name))
            d.append(('Scheduler', 'scheduler', async_task.scheduler_name))
            d.append(('VAE', 'vae', async_task.vae_name))
            d.append(('Seed', 'seed', str(task['task_seed'])))

            if async_task.freeu_enabled:
                d.append(('FreeU', 'freeu',
                          str((async_task.freeu_b1, async_task.freeu_b2, async_task.freeu_s1, async_task.freeu_s2))))

            for li, (n, w) in enumerate(async_task.loras):
                if n != 'None':
                    d.append((f'LoRA {li + 1}', f'lora_combined_{li + 1}', f'{n} : {w}'))

            metadata_parser = None
            if async_task.save_metadata_to_images:
                metadata_parser = modules.meta_parser.get_metadata_parser(async_task.metadata_scheme)
                metadata_parser.set_data(task['log_positive_prompt'], task['positive'],
                                         task['log_negative_prompt'], task['negative'],
                                         async_task.steps, async_task.base_model_name, async_task.refiner_model_name,
                                         async_task.loras, async_task.vae_name)
            d.append(('Metadata Scheme', 'metadata_scheme',
                      async_task.metadata_scheme.value if async_task.save_metadata_to_images else async_task.save_metadata_to_images))
            d.append(('Version', 'version', 'Fooocus v' + fooocus_version.version))
            img_paths.append(log(x, d, metadata_parser, async_task.output_format, task))

        return img_paths

    def apply_control_nets(async_task, height, ip_adapter_face_path, ip_adapter_path, width):
        for task in async_task.cn_tasks[flags.cn_canny]:
            cn_img, cn_stop, cn_weight = task
            cn_img = resize_image(HWC3(cn_img), width=width, height=height)

            if not async_task.skipping_cn_preprocessor:
                cn_img = preprocessors.canny_pyramid(cn_img, async_task.canny_low_threshold,
                                                     async_task.canny_high_threshold)

            cn_img = HWC3(cn_img)
            task[0] = core.numpy_to_pytorch(cn_img)
            if async_task.debugging_cn_preprocessor:
                yield_result(async_task, cn_img, async_task.black_out_nsfw, do_not_show_finished_images=True)
        for task in async_task.cn_tasks[flags.cn_cpds]:
            cn_img, cn_stop, cn_weight = task
            cn_img = resize_image(HWC3(cn_img), width=width, height=height)

            if not async_task.skipping_cn_preprocessor:
                cn_img = preprocessors.cpds(cn_img)

            cn_img = HWC3(cn_img)
            task[0] = core.numpy_to_pytorch(cn_img)
            if async_task.debugging_cn_preprocessor:
                yield_result(async_task, cn_img, async_task.black_out_nsfw, do_not_show_finished_images=True)
        for task in async_task.cn_tasks[flags.cn_ip]:
            cn_img, cn_stop, cn_weight = task
            cn_img = HWC3(cn_img)

            # https://github.com/tencent-ailab/IP-Adapter/blob/d580c50a291566bbf9fc7ac0f760506607297e6d/README.md?plain=1#L75
            cn_img = resize_image(cn_img, width=224, height=224, resize_mode=0)

            task[0] = ip_adapter.preprocess(cn_img, ip_adapter_path=ip_adapter_path)
            if async_task.debugging_cn_preprocessor:
                yield_result(async_task, cn_img, async_task.black_out_nsfw, do_not_show_finished_images=True)
        for task in async_task.cn_tasks[flags.cn_ip_face]:
            cn_img, cn_stop, cn_weight = task
            cn_img = HWC3(cn_img)

            if not async_task.skipping_cn_preprocessor:
                cn_img = extras.face_crop.crop_image(cn_img)

            # https://github.com/tencent-ailab/IP-Adapter/blob/d580c50a291566bbf9fc7ac0f760506607297e6d/README.md?plain=1#L75
            cn_img = resize_image(cn_img, width=224, height=224, resize_mode=0)

            task[0] = ip_adapter.preprocess(cn_img, ip_adapter_path=ip_adapter_face_path)
            if async_task.debugging_cn_preprocessor:
                yield_result(async_task, cn_img, async_task.black_out_nsfw, do_not_show_finished_images=True)
        all_ip_tasks = async_task.cn_tasks[flags.cn_ip] + async_task.cn_tasks[flags.cn_ip_face]
        if len(all_ip_tasks) > 0:
            pipeline.final_unet = ip_adapter.patch_model(pipeline.final_unet, all_ip_tasks)

    def apply_vary(async_task, uov_input_image, denoising_strength, switch):
        if 'subtle' in async_task.uov_method:
            async_task.denoising_strength = 0.5
        if 'strong' in async_task.uov_method:
            async_task.denoising_strength = 0.85
        if async_task.overwrite_vary_strength > 0:
            async_task.denoising_strength = async_task.overwrite_vary_strength
        shape_ceil = get_image_shape_ceil(uov_input_image)
        if shape_ceil < 1024:
            print(f'[Vary] Image is resized because it is too small.')
            shape_ceil = 1024
        elif shape_ceil > 2048:
            print(f'[Vary] Image is resized because it is too big.')
            shape_ceil = 2048
        uov_input_image = set_image_shape_ceil(uov_input_image, shape_ceil)
        initial_pixels = core.numpy_to_pytorch(uov_input_image)
        progressbar(async_task, 8, 'VAE encoding ...')
        candidate_vae, _ = pipeline.get_candidate_vae(
            steps=async_task.steps,
            switch=switch,
            denoise=denoising_strength,
            refiner_swap_method=async_task.refiner_swap_method
        )
        initial_latent = core.encode_vae(vae=candidate_vae, pixels=initial_pixels)
        B, C, H, W = initial_latent['samples'].shape
        width = W * 8
        height = H * 8
        print(f'Final resolution is {str((width, height))}.')
        return initial_latent, width, height

    def apply_inpaint(async_task, initial_latent, inpaint_head_model_path, inpaint_image,
                inpaint_mask, inpaint_parameterized, switch):
        if len(async_task.outpaint_selections) > 0:
            H, W, C = inpaint_image.shape
            if 'top' in async_task.outpaint_selections:
                inpaint_image = np.pad(inpaint_image, [[int(H * 0.3), 0], [0, 0], [0, 0]], mode='edge')
                inpaint_mask = np.pad(inpaint_mask, [[int(H * 0.3), 0], [0, 0]], mode='constant',
                                      constant_values=255)
            if 'bottom' in async_task.outpaint_selections:
                inpaint_image = np.pad(inpaint_image, [[0, int(H * 0.3)], [0, 0], [0, 0]], mode='edge')
                inpaint_mask = np.pad(inpaint_mask, [[0, int(H * 0.3)], [0, 0]], mode='constant',
                                      constant_values=255)

            H, W, C = inpaint_image.shape
            if 'left' in async_task.outpaint_selections:
                inpaint_image = np.pad(inpaint_image, [[0, 0], [int(W * 0.3), 0], [0, 0]], mode='edge')
                inpaint_mask = np.pad(inpaint_mask, [[0, 0], [int(W * 0.3), 0]], mode='constant',
                                      constant_values=255)
            if 'right' in async_task.outpaint_selections:
                inpaint_image = np.pad(inpaint_image, [[0, 0], [0, int(W * 0.3)], [0, 0]], mode='edge')
                inpaint_mask = np.pad(inpaint_mask, [[0, 0], [0, int(W * 0.3)]], mode='constant',
                                      constant_values=255)

            inpaint_image = np.ascontiguousarray(inpaint_image.copy())
            inpaint_mask = np.ascontiguousarray(inpaint_mask.copy())
            async_task.inpaint_strength = 1.0
            async_task.inpaint_respective_field = 1.0
        denoising_strength = async_task.inpaint_strength
        inpaint_worker.current_task = inpaint_worker.InpaintWorker(
            image=inpaint_image,
            mask=inpaint_mask,
            use_fill=denoising_strength > 0.99,
            k=async_task.inpaint_respective_field
        )
        if async_task.debugging_inpaint_preprocessor:
            yield_result(async_task, inpaint_worker.current_task.visualize_mask_processing(), async_task.black_out_nsfw,
                         do_not_show_finished_images=True)
            raise EarlyReturnException

        progressbar(async_task, 11, 'VAE Inpaint encoding ...')
        inpaint_pixel_fill = core.numpy_to_pytorch(inpaint_worker.current_task.interested_fill)
        inpaint_pixel_image = core.numpy_to_pytorch(inpaint_worker.current_task.interested_image)
        inpaint_pixel_mask = core.numpy_to_pytorch(inpaint_worker.current_task.interested_mask)
        candidate_vae, candidate_vae_swap = pipeline.get_candidate_vae(
            steps=async_task.steps,
            switch=switch,
            denoise=denoising_strength,
            refiner_swap_method=async_task.refiner_swap_method
        )
        latent_inpaint, latent_mask = core.encode_vae_inpaint(
            mask=inpaint_pixel_mask,
            vae=candidate_vae,
            pixels=inpaint_pixel_image)
        latent_swap = None
        if candidate_vae_swap is not None:
            progressbar(async_task, 12, 'VAE SD15 encoding ...')
            latent_swap = core.encode_vae(
                vae=candidate_vae_swap,
                pixels=inpaint_pixel_fill)['samples']
        progressbar(async_task, 13, 'VAE encoding ...')
        latent_fill = core.encode_vae(
            vae=candidate_vae,
            pixels=inpaint_pixel_fill)['samples']
        inpaint_worker.current_task.load_latent(
            latent_fill=latent_fill, latent_mask=latent_mask, latent_swap=latent_swap)
        if inpaint_parameterized:
            pipeline.final_unet = inpaint_worker.current_task.patch(
                inpaint_head_model_path=inpaint_head_model_path,
                inpaint_latent=latent_inpaint,
                inpaint_latent_mask=latent_mask,
                model=pipeline.final_unet
            )
        if not async_task.inpaint_disable_initial_latent:
            initial_latent = {'samples': latent_fill}
        B, C, H, W = latent_fill.shape
        height, width = H * 8, W * 8
        final_height, final_width = inpaint_worker.current_task.image.shape[:2]
        print(f'Final resolution is {str((final_height, final_width))}, latent is {str((width, height))}.')

        return denoising_strength, initial_latent, width, height

    def apply_upscale(async_task, switch):
        H, W, C = async_task.uov_input_image.shape
        progressbar(async_task, 9, f'Upscaling image from {str((H, W))} ...')
        async_task.uov_input_image = perform_upscale(async_task.uov_input_image)
        print(f'Image upscaled.')
        if '1.5x' in async_task.uov_method:
            f = 1.5
        elif '2x' in async_task.uov_method:
            f = 2.0
        else:
            f = 1.0
        shape_ceil = get_shape_ceil(H * f, W * f)
        if shape_ceil < 1024:
            print(f'[Upscale] Image is resized because it is too small.')
            async_task.uov_input_image = set_image_shape_ceil(async_task.uov_input_image, 1024)
            shape_ceil = 1024
        else:
            async_task.uov_input_image = resample_image(async_task.uov_input_image, width=W * f, height=H * f)
        image_is_super_large = shape_ceil > 2800
        if 'fast' in async_task.uov_method:
            direct_return = True
        elif image_is_super_large:
            print('Image is too large. Directly returned the SR image. '
                  'Usually directly return SR image at 4K resolution '
                  'yields better results than SDXL diffusion.')
            direct_return = True
        else:
            direct_return = False
        if direct_return:
            d = [('Upscale (Fast)', 'upscale_fast', '2x')]
            if modules.config.default_black_out_nsfw or async_task.black_out_nsfw:
                progressbar(async_task, 100, 'Checking for NSFW content ...')
                async_task.uov_input_image = default_censor(async_task.uov_input_image)
            progressbar(async_task, 100, 'Saving image to system ...')
            uov_input_image_path = log(async_task.uov_input_image, d, output_format=async_task.output_format)
            yield_result(async_task, uov_input_image_path, async_task.black_out_nsfw, False,
                         do_not_show_finished_images=True)
            raise EarlyReturnException

        tiled = True
        denoising_strength = 0.382
        if async_task.overwrite_upscale_strength > 0:
            denoising_strength = async_task.overwrite_upscale_strength
        initial_pixels = core.numpy_to_pytorch(async_task.uov_input_image)
        progressbar(async_task, 10, 'VAE encoding ...')
        candidate_vae, _ = pipeline.get_candidate_vae(
            steps=async_task.steps,
            switch=switch,
            denoise=denoising_strength,
            refiner_swap_method=async_task.refiner_swap_method
        )
        initial_latent = core.encode_vae(
            vae=candidate_vae,
            pixels=initial_pixels, tiled=True)
        B, C, H, W = initial_latent['samples'].shape
        width = W * 8
        height = H * 8
        print(f'Final resolution is {str((width, height))}.')
        return denoising_strength, height, initial_latent, tiled, width

    def apply_overrides(async_task, height, width):
        if async_task.overwrite_step > 0:
            async_task.steps = async_task.overwrite_step
        switch = int(round(async_task.steps * async_task.refiner_switch))
        if async_task.overwrite_switch > 0:
            switch = async_task.overwrite_switch
        if async_task.overwrite_width > 0:
            width = async_task.overwrite_width
        if async_task.overwrite_height > 0:
            height = async_task.overwrite_height
        return height, switch, width

    def process_prompt(async_task, base_model_additional_loras, use_expansion, use_style,
                    use_synthetic_refiner):
        prompts = remove_empty_str([safe_str(p) for p in async_task.prompt.splitlines()], default='')
        negative_prompts = remove_empty_str([safe_str(p) for p in async_task.negative_prompt.splitlines()], default='')
        prompt = prompts[0]
        negative_prompt = negative_prompts[0]
        if prompt == '':
            # disable expansion when empty since it is not meaningful and influences image prompt
            use_expansion = False
        extra_positive_prompts = prompts[1:] if len(prompts) > 1 else []
        extra_negative_prompts = negative_prompts[1:] if len(negative_prompts) > 1 else []
        progressbar(async_task, 2, 'Loading models ...')
        lora_filenames = modules.util.remove_performance_lora(modules.config.lora_filenames,
                                                              async_task.performance_selection)
        loras, prompt = parse_lora_references_from_prompt(prompt, async_task.loras,
                                                          modules.config.default_max_lora_number,
                                                          lora_filenames=lora_filenames)
        loras += async_task.performance_loras
        pipeline.refresh_everything(refiner_model_name=async_task.refiner_model_name,
                                    base_model_name=async_task.base_model_name,
                                    loras=loras, base_model_additional_loras=base_model_additional_loras,
                                    use_synthetic_refiner=use_synthetic_refiner, vae_name=async_task.vae_name)
        pipeline.set_clip_skip(async_task.clip_skip)
        progressbar(async_task, 3, 'Processing prompts ...')
        tasks = []
        for i in range(async_task.image_number):
            if async_task.disable_seed_increment:
                task_seed = async_task.seed % (constants.MAX_SEED + 1)
            else:
                task_seed = (async_task.seed + i) % (constants.MAX_SEED + 1)  # randint is inclusive, % is not

            task_rng = random.Random(task_seed)  # may bind to inpaint noise in the future
            task_prompt = apply_wildcards(prompt, task_rng, i, async_task.read_wildcards_in_order)
            task_prompt = apply_arrays(task_prompt, i)
            task_negative_prompt = apply_wildcards(negative_prompt, task_rng, i, async_task.read_wildcards_in_order)
            task_extra_positive_prompts = [apply_wildcards(pmt, task_rng, i, async_task.read_wildcards_in_order) for pmt
                                           in
                                           extra_positive_prompts]
            task_extra_negative_prompts = [apply_wildcards(pmt, task_rng, i, async_task.read_wildcards_in_order) for pmt
                                           in
                                           extra_negative_prompts]

            positive_basic_workloads = []
            negative_basic_workloads = []

            task_styles = async_task.style_selections.copy()
            if use_style:
                for i, s in enumerate(task_styles):
                    if s == random_style_name:
                        s = get_random_style(task_rng)
                        task_styles[i] = s
                    p, n = apply_style(s, positive=task_prompt)
                    positive_basic_workloads = positive_basic_workloads + p
                    negative_basic_workloads = negative_basic_workloads + n
            else:
                positive_basic_workloads.append(task_prompt)

            negative_basic_workloads.append(task_negative_prompt)  # Always use independent workload for negative.

            positive_basic_workloads = positive_basic_workloads + task_extra_positive_prompts
            negative_basic_workloads = negative_basic_workloads + task_extra_negative_prompts

            positive_basic_workloads = remove_empty_str(positive_basic_workloads, default=task_prompt)
            negative_basic_workloads = remove_empty_str(negative_basic_workloads, default=task_negative_prompt)

            tasks.append(dict(
                task_seed=task_seed,
                task_prompt=task_prompt,
                task_negative_prompt=task_negative_prompt,
                positive=positive_basic_workloads,
                negative=negative_basic_workloads,
                expansion='',
                c=None,
                uc=None,
                positive_top_k=len(positive_basic_workloads),
                negative_top_k=len(negative_basic_workloads),
                log_positive_prompt='\n'.join([task_prompt] + task_extra_positive_prompts),
                log_negative_prompt='\n'.join([task_negative_prompt] + task_extra_negative_prompts),
                styles=task_styles
            ))
        if use_expansion:
            for i, t in enumerate(tasks):
                progressbar(async_task, 4, f'Preparing Fooocus text #{i + 1} ...')
                expansion = pipeline.final_expansion(t['task_prompt'], t['task_seed'])
                print(f'[Prompt Expansion] {expansion}')
                t['expansion'] = expansion
                t['positive'] = copy.deepcopy(t['positive']) + [expansion]  # Deep copy.
        for i, t in enumerate(tasks):
            progressbar(async_task, 5, f'Encoding positive #{i + 1} ...')
            t['c'] = pipeline.clip_encode(texts=t['positive'], pool_top_k=t['positive_top_k'])
        for i, t in enumerate(tasks):
            if abs(float(async_task.cfg_scale) - 1.0) < 1e-4:
                t['uc'] = pipeline.clone_cond(t['c'])
            else:
                progressbar(async_task, 6, f'Encoding negative #{i + 1} ...')
                t['uc'] = pipeline.clip_encode(texts=t['negative'], pool_top_k=t['negative_top_k'])
        return tasks, use_expansion

    def apply_freeu(async_task):
        print(f'FreeU is enabled!')
        pipeline.final_unet = core.apply_freeu(
            pipeline.final_unet,
            async_task.freeu_b1,
            async_task.freeu_b2,
            async_task.freeu_s1,
            async_task.freeu_s2
        )

    def patch_discrete(unet, scheduler_name):
        return core.opModelSamplingDiscrete.patch(unet, scheduler_name, False)[0]

    def patch_edm(unet, scheduler_name):
        return core.opModelSamplingContinuousEDM.patch(unet, scheduler_name, 120.0, 0.002)[0]

    def patch_samplers(async_task):
        final_scheduler_name = async_task.scheduler_name

        if async_task.scheduler_name in ['lcm', 'tcd']:
            final_scheduler_name = 'sgm_uniform'
            if pipeline.final_unet is not None:
                pipeline.final_unet = patch_discrete(pipeline.final_unet, async_task.scheduler_name)
            if pipeline.final_refiner_unet is not None:
                pipeline.final_refiner_unet = patch_discrete(pipeline.final_refiner_unet, async_task.scheduler_name)

        elif async_task.scheduler_name == 'edm_playground_v2.5':
            final_scheduler_name = 'karras'
            if pipeline.final_unet is not None:
                pipeline.final_unet = patch_edm(pipeline.final_unet, async_task.scheduler_name)
            if pipeline.final_refiner_unet is not None:
                pipeline.final_refiner_unet = patch_edm(pipeline.final_refiner_unet, async_task.scheduler_name)

        return final_scheduler_name

    def translate_prompts(async_task):
        from modules.translator import translate2en
        async_task.prompt = translate2en(async_task.prompt, 'prompt')
        async_task.negative_prompt = translate2en(async_task.negative_prompt, 'negative prompt')

    def set_hyper_sd_defaults(async_task):
        print('Enter Hyper-SD mode.')
        progressbar(async_task, 1, 'Downloading Hyper-SD components ...')
        async_task.performance_loras += [(modules.config.downloading_sdxl_hyper_sd_lora(), 0.8)]
        if async_task.refiner_model_name != 'None':
            print(f'Refiner disabled in Hyper-SD mode.')
        async_task.refiner_model_name = 'None'
        async_task.sampler_name = 'dpmpp_sde_gpu'
        async_task.scheduler_name = 'karras'
        async_task.sharpness = 0.0
        async_task.cfg_scale = 1.0
        async_task.adaptive_cfg = 1.0
        async_task.refiner_switch = 1.0
        async_task.adm_scaler_positive = 1.0
        async_task.adm_scaler_negative = 1.0
        async_task.adm_scaler_end = 0.0

    def set_lightning_defaults(async_task):
        print('Enter Lightning mode.')
        progressbar(async_task, 1, 'Downloading Lightning components ...')
        async_task.performance_loras += [(modules.config.downloading_sdxl_lightning_lora(), 1.0)]
        if async_task.refiner_model_name != 'None':
            print(f'Refiner disabled in Lightning mode.')
        async_task.refiner_model_name = 'None'
        async_task.sampler_name = 'euler'
        async_task.scheduler_name = 'sgm_uniform'
        async_task.sharpness = 0.0
        async_task.cfg_scale = 1.0
        async_task.adaptive_cfg = 1.0
        async_task.refiner_switch = 1.0
        async_task.adm_scaler_positive = 1.0
        async_task.adm_scaler_negative = 1.0
        async_task.adm_scaler_end = 0.0

    def set_lcm_defaults(async_task):
        print('Enter LCM mode.')
        progressbar(async_task, 1, 'Downloading LCM components ...')
        async_task.performance_loras += [(modules.config.downloading_sdxl_lcm_lora(), 1.0)]
        if async_task.refiner_model_name != 'None':
            print(f'Refiner disabled in LCM mode.')
        async_task.refiner_model_name = 'None'
        async_task.sampler_name = 'lcm'
        async_task.scheduler_name = 'lcm'
        async_task.sharpness = 0.0
        async_task.cfg_scale = 1.0
        async_task.adaptive_cfg = 1.0
        async_task.refiner_switch = 1.0
        async_task.adm_scaler_positive = 1.0
        async_task.adm_scaler_negative = 1.0
        async_task.adm_scaler_end = 0.0

    while True:
        time.sleep(0.01)
        if len(async_tasks) > 0:
            task = async_tasks.pop(0)

            try:
                handler(task)
                if task.generate_image_grid:
                    build_image_wall(task)
                task.yields.append(['finish', task.results])
                pipeline.prepare_text_encoder(async_call=True)
            except:
                traceback.print_exc()
                task.yields.append(['finish', task.results])
            finally:
                if pid in modules.patch.patch_settings:
                    del modules.patch.patch_settings[pid]
    pass


threading.Thread(target=worker, daemon=True).start()
