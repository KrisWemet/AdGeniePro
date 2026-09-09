import random

class AdCopyAgent:
    """
    Generates ad copy given basic product_info like:
    {
      "product_name": "Some Product"
    }
    """

    def run(self, product_info):
        # Hook Options
        hooks = [
            f"🚀 Still struggling with {product_info['product_name']} that doesn’t deliver?",
            f"🔥 Ever wish your {product_info['product_name']} could actually make life easier?",
            f"💡 What if {product_info['product_name']} could help you feel amazing today?"
        ]

        # Emotional Triggers (Storytelling)
        stories = [
            f"Meet Jake. He was frustrated with {product_info['product_name']} that never worked – until he found this.",
            f"Sarah couldn’t believe how much easier life became after switching to this {product_info['product_name']}.",
            f"You’ve probably tried other {product_info['product_name']} before, but nothing like this."
        ]

        # Social Proof
        social_proof = [
            f"🎯 Over 10,000 happy customers trust this {product_info['product_name']}.",
            f"💼 Featured in top magazines – this {product_info['product_name']} is making waves.",
            f"⭐ Rated 4.9 by thousands who say this changed their life!"
        ]

        # Call to Action
        call_to_action = [
            "Don’t wait – transform your experience today! 💪",
            "Ready to see the difference? Act now! 🎯",
            "Click below and start your journey to better results! ✅"
        ]

        # Random Selection
        hook = random.choice(hooks)
        story = random.choice(stories)
        proof = random.choice(social_proof)
        cta = random.choice(call_to_action)

        # Combine into final ad copy
        return f"{hook}\n\n{story}\n{proof}\n\n{cta}"


if __name__ == "__main__":
    # Test it directly
    product = {"product_name": "Fitness Tracker"}
    generated_copy = AdCopyAgent().run(product)
    print("Generated Ad Copy:\n", generated_copy)
