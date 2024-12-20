# Adopted from https://github.com/lm-sys/FastChat. Below is the original copyright:
# Adopted from tatsu-lab@stanford_alpaca. Below is the original copyright:
#    Copyright 2023 Rohan Taori, Ishaan Gulrajani, Tianyi Zhang, Yann Dubois, Xuechen Li
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.


from transformers import SiglipImageProcessor
from peft import LoraConfig, get_peft_model
import warnings
import copy


from instructseg.datasets.InstructSegDatasets import *
from .llava_trainer import LLaVATrainer
from instructseg.model.language_model.llava_phi import InstructSeg

warnings.filterwarnings('ignore')
local_rank = None



@dataclass
class ModelArguments:

    model_name_or_path: Optional[str] = field(default="pretrained_model/mllm/Mipha-3B")
   
    version: Optional[str] = field(default="phi-2")

    freeze_backbone: bool = field(default=False)
    train_clip_backbone: bool = field(default=False)
    train_swin_backbone: bool = field(default=False)

    vision_tower: str = "pretrained_model/CLIP/siglip-so400m-patch14-384"
    vision_tower_mask: str = "pretrained_model/mask2former/maskformer2_swin_base_IN21k_384_bs16_50ep.pkl"
    with_norm: bool = field(default=True)
    with_layernorm: bool = field(default=False)
    skip_init_vision: bool = field(default=False)
    swin_type: Optional[str] = field(default="base")
    projector_outdim: Optional[int] = field(default=2048)
    mm_projector_type: Optional[str] = field(default="swin_conv")
    model_version: Optional[str] = field(default="v1")
    load_mask2former: bool = field(default=True)
    seg_task: Optional[str] = field(default="referring")
    mask_config: Optional[str] = field(default="instructseg/model/mask_decoder/mask_config/maskformer2_swin_base_384_bs16_50ep.yaml")


@dataclass
class DataArguments:


    lazy_preprocess: bool = True
    is_multimodal: bool = False
    image_folder: Optional[str] = field(default='dataset/coco')
    refcoco_image_folder: Optional[str] = "dataset/coco/train2014"
    image_first: bool = field(default=True)
    seg_last: bool = field(default=True)
    instruction_version: str = 'v1'
    image_aspect_ratio: str = 'square'
    image_grid_pinpoints: Optional[str] = field(default=None)
    ref_coco_path: str = 'dataset/RES/refcoco/refcoco_train.json'
    ref_coco_plus_path: str = 'dataset/RES/refcoco+/refcoco+_train.json'
    ref_coco_g_path: str = 'dataset/RES/refcocog/refcocog_train.json'
    mmconv_path: str = 'dataset/llava_dataset'

    youtube_image_path: str = 'dataset/rvos/YouTube/train/JPEGImages'
    davis_image_path: str = 'dataset/rvos/DAVIS/train/JPEGImages'

    refyoutube_json_path: str = 'dataset/rvos/YouTube/train/refyoutube_train.json'

    # reason seg
    reason_path : str = 'dataset/ReasonSeg'
    reason_seg_data: str = 'ReasonSeg|train'
    explanatory: float = 0.1

    # reasonVOS 
    reason_vos_path: str = 'dataset/ReVOS'

    # refercoco vqa rvos reasonseg reasonVOS
    data_ratio: str = '4||2||4||1||3'  
    switch_bs: int = 4 # 16
    fix_dataset_len: int = 0
    segmentation: bool = True
    data_aug: bool = False
    reference_frame_num: int =  4

    # enable OVP module in the paper
    use_temporal_query:  bool = True
    # enable VMTF module in the paper
    use_vmtf: bool = True



    

@dataclass
class TrainingArguments(transformers.TrainingArguments):
    
    dataloader_prefetch_factor: int = field(default=2)
    dataloader_num_workers: int = field(default=4)
    num_train_epochs: int = field(default=10)
    per_device_train_batch_size: int = field(default=2)
    per_device_eval_batch_size: int = field(default=2)
    gradient_accumulation_steps: int = field(default=1)
    gradient_checkpointing: bool = field(default=True)
    deepspeed: Optional[str] = field(default='scripts/seg/zero1.json')
    
    output_dir: Optional[str] = field(default="output/model")
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    remove_unused_columns: bool = field(default=False)
    freeze_mm_mlp_adapter: bool = field(default=True)
    mpt_attn_impl: Optional[str] = field(default="triton")
    model_max_length: int = field(
        default=2048,
        metadata={
            "help":
                "Maximum sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )
    double_quant: bool = field(
        default=True,
        metadata={"help": "Compress the quantization statistics through double quantization."}
    )
    quant_type: str = field(
        default="nf4",
        metadata={"help": "Quantization data type to use. Should be one of `fp4` or `nf4`."}
    )
    bits: int = field(
        default=16,
        metadata={"help": "How many bits to use."}
    )
    lora_enable: bool = True
    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_weight_path: str = ""
    lora_bias: str = "none"
    dataloader_drop_last: bool = True


def maybe_zero_3(param, ignore_status=False, name=None):
    from deepspeed import zero
    from deepspeed.runtime.zero.partition_parameters import ZeroParamStatus
    if hasattr(param, "ds_id"):
        if param.ds_status == ZeroParamStatus.NOT_AVAILABLE:
            if not ignore_status:
                logging.warning(f"{name}: param.ds_status != ZeroParamStatus.NOT_AVAILABLE: {param.ds_status}")
        with zero.GatheredParameters([param]):
            param = param.data.detach().cpu().clone()
    else:
        param = param.detach().cpu().clone()
    return param


def get_mm_adapter_state_maybe_zero_3(named_params, keys_to_match):
    to_return = {k: t for k, t in named_params if any(key_match in k for key_match in keys_to_match)}
    to_return = {k: maybe_zero_3(v, ignore_status=True).cpu() for k, v in to_return.items()}
    return to_return


def find_linear_layers(model, lora_target_modules=['q_proj', 'v_proj'], train_module_list=[]): 
    cur_train_module_list = copy.deepcopy(train_module_list)
    cur_train_module_list.extend(["vision_tower", "vision_tower_mask"])
    cls = torch.nn.Linear
    lora_module_names = set()
    for name, module in model.named_modules():
        if (isinstance(module, cls)
            and all(
                        [
                            x not in name
                            for x in cur_train_module_list
                        ]
                    )
                    and any([x in name for x in lora_target_modules])):
            # names = name.split('.')
            # lora_module_names.add(names[0] if len(names) == 1 else names[-1])
            lora_module_names.add(name)

    return sorted(list(lora_module_names))


def safe_save_model_for_hf_trainer(trainer: transformers.Trainer,
                                   output_dir: str):
    """Collects the state dict and dump to disk."""


    if trainer.deepspeed:
        torch.cuda.synchronize()
        trainer.save_model(output_dir)
        return

    state_dict = trainer.model.state_dict()
    if trainer.args.should_save:
        cpu_state_dict = {
            key: value.cpu()
            for key, value in state_dict.items()
        }
        del state_dict
        trainer._save(output_dir, state_dict=cpu_state_dict)  # noqa


def smart_tokenizer_and_embedding_resize(
        special_tokens_dict: Dict,
        tokenizer: transformers.PreTrainedTokenizer,
        model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg




def make_unify_datamodule(clip_image_processor, tokenizer, data_args, training_args):
    data_ratio = data_args.data_ratio
    data_ratio = data_ratio.split('||')
    data_ratio = [int(data_) for data_ in data_ratio]
    datasets = []

    if data_ratio[0] != 0:
        referring_json_path = [data_args.ref_coco_path, data_args.ref_coco_plus_path, data_args.ref_coco_g_path]
        refcoco_dataset = RefCOCO_dataset(json_path=referring_json_path, tokenizer=tokenizer, data_args=data_args)
        datasets += [refcoco_dataset] * data_ratio[0]

    if data_ratio[1] != 0:
        mm_conv_json = os.path.join(data_args.mmconv_path,'llava_v1_5_mix665k_onlyMM_filtered.json')
        mm_conv_dataset = MM_Conv_Dataset(data_path=mm_conv_json, tokenizer=tokenizer,
                                                             data_args=data_args)
        datasets += [mm_conv_dataset] * data_ratio[1]
    
    if data_ratio[2] != 0:
        ref_vos_dataset = REF_VOS_dataset_train(json_path=data_args.refyoutube_json_path, image_path_yv=data_args.youtube_image_path, image_path_davis=data_args.davis_image_path, tokenizer=tokenizer,
            clip_image_processor=clip_image_processor, data_args=data_args)
        datasets += [ref_vos_dataset] * data_ratio[2]
    
    if data_ratio[3] != 0:
        reason_dataset = Reason_dataset(reason_path=data_args.reason_path, tokenizer=tokenizer, data_args=data_args)
        datasets += [reason_dataset] * data_ratio[3]

    if data_ratio[4] != 0:
        reason_vos_dataset = Reason_VOS_dataset(revos_path=data_args.reason_vos_path, tokenizer=tokenizer, data_args=data_args)
        datasets += [reason_vos_dataset] * data_ratio[4]
    
    print(f'the dataset ratio is: {data_ratio}')

    # you can change 16 to your frequency sets, it represents how many samples to change tasks
    train_dataset = UnifyDatasetSingleDatasetForBatch(datasets,data_ratio,data_args.switch_bs,fix_dataset_len=data_args.fix_dataset_len)
    print(f'total unify dataset number is {len(train_dataset)}')
    data_collator = DataCollatorForCOCODatasetV2(tokenizer=tokenizer, clip_image_processor=clip_image_processor)
    return dict(train_dataset=train_dataset, eval_dataset=None, data_collator=data_collator)

def train():
    global local_rank

    parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    local_rank = training_args.local_rank
    compute_dtype = (torch.float16 if training_args.fp16 else (torch.bfloat16 if training_args.bf16 else torch.float32))

    mask_cfg = get_mask_config(config=model_args.mask_config)
    bnb_model_from_pretrained_args = {}

    # training_args.bf16 = True

    # if not training_args.bf16:
    model = InstructSeg.from_pretrained(
        model_args.model_name_or_path,
        mask_decoder_cfg=mask_cfg,
        add_cross_attn=True,
        cache_dir=training_args.cache_dir,
        **bnb_model_from_pretrained_args
                )
    
    model.use_temporal_query = data_args.use_temporal_query
    model.use_vmtf = data_args.use_vmtf


    if not model.is_train_mask_decode:
        mask2former_ckpt = model_args.vision_tower_mask if model_args.load_mask2former else None
        model.initial_mask_module(mask2former_ckpt, model_args)

    model.config.use_cache = False

    if model_args.freeze_backbone:
        model.model.requires_grad_(False)


    if training_args.gradient_checkpointing:
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
        else:
            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)

            model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=training_args.cache_dir,
        model_max_length=training_args.model_max_length,
        padding_side="right",
        use_fast=False,
    )

    if tokenizer.pad_token is None:
        smart_tokenizer_and_embedding_resize(
            special_tokens_dict=dict(pad_token="[PAD]"),
            tokenizer=tokenizer,
            model=model,
        )
    if model_args.version in conversation_lib.conv_templates:
        conversation_lib.default_conversation = conversation_lib.conv_templates[model_args.version]
    else:
        conversation_lib.default_conversation = conversation_lib.conv_templates["vicuna_v1"]

    if model_args.vision_tower is not None:
        model.get_model().initialize_vision_modules(
            model_args=model_args,
            fsdp=training_args.fsdp
        )

        vision_tower = model.get_vision_tower()
        vision_tower_mask = model.model.get_vision_tower_mask()
        vision_tower.to(dtype=torch.float16 if training_args.fp16 else (torch.bfloat16 if training_args.bf16 else torch.float32), device=training_args.device)
        vision_tower_mask.to(dtype=torch.float16 if training_args.fp16 else (torch.bfloat16 if training_args.bf16 else torch.float32), device=training_args.device)
        data_args.image_processor = vision_tower_mask.image_processor
        data_args.is_multimodal = True

        model.config.image_aspect_ratio = data_args.image_aspect_ratio
        model.config.image_grid_pinpoints = data_args.image_grid_pinpoints

        
        if not model_args.train_clip_backbone:
            model.model.vision_tower.requires_grad_(False)
        if not model_args.train_swin_backbone:
            model.model.vision_tower_mask.requires_grad_(False)


        model.config.freeze_mm_mlp_adapter = training_args.freeze_mm_mlp_adapter
        if training_args.freeze_mm_mlp_adapter:
            for p in model.get_model().mm_projector.parameters():
                p.requires_grad = False

        model.initialize_vision_tokenizer(model_args, tokenizer=tokenizer)


    tokenizer.add_tokens("[SEG]")
    model.resize_token_embeddings(len(tokenizer))
    train_module_list = ["seg_query", "embed_tokens", "lm_head", 
        "temporal_query",
        "pixel_decoder", "predictor", 
        "seg_query_projector", "SEG_token_projector",
        "temporal_query_project",
        'ovp_layers', 'vmtf_layers', 'text_vmtf_projector', 'origin_SEG_token_projector',
        'local_project',]


    if model_args.train_swin_backbone:
        train_module_list.append('vision_tower_mask')
        
    if training_args.lora_enable:
        lora_r = training_args.lora_r
        lora_alpha = training_args.lora_alpha
        lora_dropout = training_args.lora_dropout
        lora_target_modules = find_linear_layers(model, train_module_list=train_module_list)
        lora_config = LoraConfig(
            r=lora_r,
            lora_alpha=lora_alpha,
            target_modules=lora_target_modules,
            lora_dropout=lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, lora_config)
        model.print_trainable_parameters()

        for n, p in model.named_parameters():
            if any(
                [
                    x in n
                    for x in train_module_list
                ]):

                p.requires_grad = True

    if training_args.local_rank == 0:
        for n, p in model.named_parameters():
            if p.requires_grad: print("n: ", n, "p.shape: ", p.shape)

    model.get_special_token(SEG=tokenizer("[SEG]", return_tensors='pt', add_special_tokens=False)['input_ids'], EOS=tokenizer.eos_token_id)
    clip_image_processor = SiglipImageProcessor.from_pretrained(model_args.vision_tower)
    data_module = make_unify_datamodule(clip_image_processor=clip_image_processor, tokenizer=tokenizer, data_args=data_args, training_args=training_args)
    training_args.dataloader_drop_last = True
    trainer = LLaVATrainer(model=model,
                           tokenizer=tokenizer,
                           args=training_args,
                           **data_module)
    if list(pathlib.Path(training_args.output_dir).glob("checkpoint-*")):
        trainer.train(resume_from_checkpoint=True)
    else:
        trainer.train()
    trainer.save_state()

    model.config.use_cache = True

    safe_save_model_for_hf_trainer(trainer=trainer,
                                       output_dir=training_args.output_dir)


if __name__ == "__main__":
    train()
