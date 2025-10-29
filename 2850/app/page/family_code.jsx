const { useState, useEffect } = React;

function FamilyCode() {
    const [family, setFamily] = useState(null);
    const [isLoading, setIsLoading] = useState(true);
    const [message, setMessage] = useState('');

    useEffect(() => {
        // 从URL参数获取家庭信息
        const urlParams = new URLSearchParams(window.location.search);
        const familyId = urlParams.get('family_id');
        const familyName = urlParams.get('family_name');
        const familyCode = urlParams.get('family_code');

        if (familyId && familyName && familyCode) {
            setFamily({ family_id: familyId, family_name: familyName, family_code: familyCode });
        } else {
            // 从localStorage获取
            const storedFamilyId = localStorage.getItem('family_id');
            const storedFamilyName = localStorage.getItem('family_name');
            const storedFamilyCode = localStorage.getItem('family_code');

            if (storedFamilyId && storedFamilyName && storedFamilyCode) {
                setFamily({ 
                    family_id: storedFamilyId, 
                    family_name: storedFamilyName, 
                    family_code: storedFamilyCode 
                });
            } else {
                setMessage('No family information found. Please create or join a family first.');
            }
        }
        
        setIsLoading(false);
    }, []);

    const copyToClipboard = async (text) => {
        try {
            await navigator.clipboard.writeText(text);
            setMessage('Family code copied to clipboard!');
            setTimeout(() => setMessage(''), 3000);
        } catch (err) {
            console.error('Failed to copy: ', err);
            setMessage('Failed to copy to clipboard');
        }
    };

    const shareFamilyCode = () => {
        if (family) {
            const shareUrl = `${window.location.origin}/page/family_code.html?family_id=${family.family_id}&family_name=${encodeURIComponent(family.family_name)}&family_code=${family.family_code}`;
            copyToClipboard(shareUrl);
        }
    };

    if (isLoading) {
        return (
            <div className="min-h-screen flex items-center justify-center">
                <div className="text-white text-xl">Loading...</div>
            </div>
        );
    }

    if (!family) {
        return (
            <div className="min-h-screen flex items-center justify-center">
                <div className="max-w-md w-full mx-4">
                    <div className="bg-white rounded-lg shadow-xl p-8 text-center">
                        <div className="text-6xl mb-6">❌</div>
                        <h1 className="text-2xl font-bold text-gray-800 mb-4">No Family Found</h1>
                        <p className="text-gray-600 mb-6">Please create or join a family first</p>
                        <a href="family_select.html" className="bg-blue-500 text-white px-6 py-2 rounded-md hover:bg-blue-600">
                            Go to Family Selection
                        </a>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen py-8">
            <div className="max-w-2xl mx-auto px-4">
                <div className="bg-white rounded-lg shadow-xl p-8">
                    <div className="text-center mb-8">
                        <h1 className="text-3xl font-bold text-gray-800 mb-2">
                            👨‍👩‍👧‍👦 Family Code
                        </h1>
                        <p className="text-gray-600">
                            Share this code with your family members
                        </p>
                    </div>

                    {/* Message */}
                    {message && (
                        <div className={`mb-6 p-4 rounded-md ${
                            message.includes('Error') || message.includes('Failed')
                                ? 'bg-red-100 text-red-700'
                                : 'bg-green-100 text-green-700'
                        }`}>
                            {message}
                        </div>
                    )}

                    {/* Family Info */}
                    <div className="bg-gray-50 rounded-lg p-6 mb-6">
                        <div className="text-center">
                            <h2 className="text-xl font-semibold text-gray-800 mb-2">
                                {family.family_name}
                            </h2>
                            <div className="text-4xl font-bold text-blue-600 mb-4">
                                {family.family_code}
                            </div>
                            <p className="text-gray-600 text-sm">
                                Share this 8-character code with your family members
                            </p>
                        </div>
                    </div>

                    {/* Action Buttons */}
                    <div className="space-y-4">
                        <button
                            onClick={() => copyToClipboard(family.family_code)}
                            className="w-full bg-blue-500 text-white py-3 px-4 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        >
                            📋 Copy Family Code
                        </button>
                        
                        <button
                            onClick={shareFamilyCode}
                            className="w-full bg-green-500 text-white py-3 px-4 rounded-md hover:bg-green-600 focus:outline-none focus:ring-2 focus:ring-green-500"
                        >
                            🔗 Share Family Link
                        </button>
                    </div>

                    {/* Instructions */}
                    <div className="mt-8 bg-blue-50 rounded-lg p-4">
                        <h3 className="font-semibold text-blue-800 mb-2">How to share:</h3>
                        <ul className="text-sm text-blue-700 space-y-1">
                            <li>• Send the 8-character family code to your family members</li>
                            <li>• Or share the family link for easy access</li>
                            <li>• Family members can join using the code</li>
                            <li>• Each person submits their own preferences</li>
                        </ul>
                    </div>

                    {/* Navigation */}
                    <div className="mt-6 flex justify-between">
                        <a href="family_select.html" className="text-blue-600 hover:text-blue-800">
                            ← Switch Family
                        </a>
                        <a href="preference_submit.html" className="text-green-600 hover:text-green-800">
                            Submit Preferences →
                        </a>
                    </div>
                </div>
            </div>
        </div>
    );
}

ReactDOM.render(<FamilyCode />, document.getElementById('root'));

