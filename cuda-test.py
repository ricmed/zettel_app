import torch

if torch.cuda.is_available():
    print(f"CUDA Disponível: {torch.cuda.is_available()}")
    print(f"Dispositivo: {torch.cuda.get_device_name(0)}")
    # PyTorch não dá o número exato de CUDA cores diretamente, 
    # apenas o nome. Ex: 'NVIDIA GeForce RTX 3060'
else:
    print("CUDA não está disponível.")

print("\n\n")
print(torch.__version__, torch.version.cuda)